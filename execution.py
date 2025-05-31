import sys
import logging
import time
import traceback
import inspect
from enum import Enum

import ray

import nodes
from metrics import REQUEST_COUNT, REQUEST_LATENCY, EXECUTION_LATENCY

from comfy_execution.graph import get_input_info, StageList, ExecutionBlocker
from comfy_execution.graph_utils import is_link, GraphBuilder
from comfy_execution.validation import validate_node_input

class ExecutionResult(Enum):
    STAGED = 0
    FAILURE = 1

class DuplicateNodeError(Exception):
    pass

class IsChangedCache:
    def __init__(self, dynprompt):
        self.dynprompt = dynprompt
        self.is_changed = {}

    def get(self, node_id):
        if node_id in self.is_changed:
            return self.is_changed[node_id]

        node = self.dynprompt.get_node(node_id)
        class_type = node["class_type"]
        class_def = nodes.NODE_CLASS_MAPPINGS[class_type]
        if not hasattr(class_def, "IS_CHANGED"):
            self.is_changed[node_id] = False
            return self.is_changed[node_id]

        if "is_changed" in node:
            self.is_changed[node_id] = node["is_changed"]
            return self.is_changed[node_id]

        # Intentionally do not use cached outputs here. We only want constants in IS_CHANGED
        input_data_all, _ = get_input_data(node["inputs"], class_def, node_id, None)
        try:
            is_changed = _map_node_over_list(class_def, "IS_CHANGED", input_data_all)
            node["is_changed"] = [None if isinstance(x, ExecutionBlocker) else x for x in is_changed]
        except Exception as e:
            logging.warning("WARNING: {}".format(e))
            node["is_changed"] = float("NaN")
        finally:
            self.is_changed[node_id] = node["is_changed"]
        return self.is_changed[node_id]

def get_input_data(inputs, class_def, unique_id, outputs=None, prompt=None, extra_data={}):
    valid_inputs = class_def.INPUT_TYPES()
    input_data_all = {}
    missing_keys = {}
    for x in inputs:
        input_data = inputs[x]
        _, input_category, input_info = get_input_info(class_def, x, valid_inputs)
        def mark_missing():
            missing_keys[x] = True
            input_data_all[x] = (None,)
        if is_link(input_data) and (not input_info or not input_info.get("rawLink", False)):
            input_unique_id = input_data[0]
            output_index = input_data[1]
            if outputs is None:
                mark_missing()
                continue # This might be a lazily-evaluated input
            cached_output = outputs.get(input_unique_id)
            if cached_output is None:
                mark_missing()
                continue
            if output_index >= len(cached_output):
                mark_missing()
                continue
            obj = cached_output[output_index]
            input_data_all[x] = obj
        elif input_category is not None:
            input_data_all[x] = [input_data]

    if "hidden" in valid_inputs:
        h = valid_inputs["hidden"]
        for x in h:
            if h[x] == "PROMPT":
                input_data_all[x] = [prompt if prompt is not None else {}]
            if h[x] == "EXTRA_PNGINFO":
                input_data_all[x] = [extra_data.get('extra_pnginfo', None)]
            if h[x] == "UNIQUE_ID":
                input_data_all[x] = [unique_id]
            if h[x] == "AUTH_TOKEN_COMFY_ORG":
                input_data_all[x] = [extra_data.get("auth_token_comfy_org", None)]
            if h[x] == "API_KEY_COMFY_ORG":
                input_data_all[x] = [extra_data.get("api_key_comfy_org", None)]
    return input_data_all, missing_keys

map_node_over_list = None #Don't hook this please

def _map_node_over_list(
    obj,
    func,
    input_data_all,
    sig=None,
    execution_block_cb=None,
    pre_execute_cb=None,
    gpu_pool=None,
):
    # check if node wants the lists
    input_is_list = getattr(obj, "INPUT_IS_LIST", False)

    if len(input_data_all) == 0:
        max_len_input = 0
    else:
        max_len_input = max(len(x) for x in input_data_all.values())

    # get a slice of inputs, repeat last input when list isn't long enough
    def slice_dict(d, i):
        return {k: v[i if len(v) > i else -1] for k, v in d.items()}

    results = []
    def process_inputs(inputs, index=None, input_is_list=False):
        execution_block = None
        for k, v in inputs.items():
            if input_is_list:
                for e in v:
                    if isinstance(e, ExecutionBlocker):
                        v = e
                        break
            if isinstance(v, ExecutionBlocker):
                execution_block = execution_block_cb(v) if execution_block_cb else v
                break
        if execution_block is None:
            if pre_execute_cb is not None and index is not None:
                pre_execute_cb(index)

            if func in {"IS_CHANGED", "VALIDATE_INPUTS"}:
                results.append(getattr(obj, func)(**inputs))
            else:
                if gpu_pool is not None and gpu_pool.should_execute(obj):
                    results.append(gpu_pool.execute(obj, func, inputs, sig))
                else:
                    results.append(getattr(obj, func).remote(**{"self": obj, **inputs}))
        else:
            results.append(execution_block)

    if input_is_list:
        process_inputs(input_data_all, 0, input_is_list=input_is_list)
    elif max_len_input == 0:
        process_inputs({})
    else:
        for i in range(max_len_input):
            input_dict = slice_dict(input_data_all, i)
            process_inputs(input_dict, i)
    return results

def merge_result_data(results, obj):
    # check which outputs need concatenating
    output = []
    output_is_list = [False] * len(results[0])
    if hasattr(obj, "OUTPUT_IS_LIST"):
        output_is_list = obj.OUTPUT_IS_LIST

    # merge node execution results
    for i, is_list in zip(range(len(results[0])), output_is_list):
        if is_list:
            value = []
            for o in results:
                if isinstance(o[i], ExecutionBlocker):
                    value.append(o[i])
                else:
                    value.extend(o[i])
            output.append(value)
        else:
            output.append([o[i] for o in results])
    return output

def get_output_data(
    obj,
    input_data_all,
    sig,
    execution_block_cb=None,
    pre_execute_cb=None,
    gpu_pool=None,
):
    results = []
    return_values = _map_node_over_list(
        obj,
        obj.FUNCTION,
        input_data_all,
        sig=sig,
        execution_block_cb=execution_block_cb,
        pre_execute_cb=pre_execute_cb,
        gpu_pool=gpu_pool,
    )

    for i in range(len(return_values)):
        r = return_values[i]
        if isinstance(r, ExecutionBlocker):
            r = tuple([r] * len(obj.RETURN_TYPES))
        results.append(r)

    if len(results) > 0:
        output = merge_result_data(results, obj)
    else:
        output = []

    return output

def format_value(x):
    if x is None:
        return None
    elif isinstance(x, (int, float, bool, str)):
        return x
    else:
        return str(x)

def execute_node(
    client_id,
    prompt_id,
    node_id,
    prompt,
    sigs,
    outputs,
    staged,
    gpu_pool,
    messages,
    extra_data,
):
    class_type = prompt[node_id]["class_type"]
    meta = {
        "client_id": client_id,
        "prompt_id": prompt_id,
        "node_id": node_id,
        "class_type": class_type,
    }

    if outputs.get(node_id) is not None:
        if client_id is not None:
            messages.add.remote("node_cached", meta, client_id)
        return (ExecutionResult.STAGED, None, None)

    inputs = prompt[node_id]["inputs"]
    input_data_all = None
    class_def = nodes.NODE_CLASS_MAPPINGS[class_type]

    try:
        input_data_all, _ = get_input_data(inputs, class_def, node_id, outputs, prompt, extra_data)
        obj = class_def()

        def execution_block_cb(block):
            if block.message is not None:
                messages.add.remote("execution_failed", meta, client_id)
                return ExecutionBlocker(None)
            else:
                return block

        def pre_execute_cb(call_index):
            GraphBuilder.set_default_prefix(node_id, call_index, 0)

        output_data = get_output_data(
            obj,
            input_data_all,
            sigs[node_id],
            execution_block_cb=execution_block_cb,
            pre_execute_cb=pre_execute_cb,
            gpu_pool=gpu_pool,
        )

        outputs[node_id] = output_data
    except Exception as ex:
        typ, _, tb = sys.exc_info()
        exception_type = full_type_name(typ)
        input_data_formatted = {}
        if input_data_all is not None:
            input_data_formatted = {}
            for name, inputs in input_data_all.items():
                input_data_formatted[name] = [format_value(x) for x in inputs]

        error_details = {
            "node_id": node_id,
            "exception_message": str(ex),
            "exception_type": exception_type,
            "traceback": traceback.format_tb(tb),
            "current_inputs": input_data_formatted,
        }

        return (ExecutionResult.FAILURE, error_details, ex)

    staged.add(node_id)

    return (ExecutionResult.STAGED, None, None)


@ray.remote(num_cpus=0, resources={"num_parallelism": 1})
def execute_prompt(
    client_id,
    prompt_id,
    prompt,
    execute_outputs,
    gpu_pool,
    cache,
    messages,
    extra_data={},
):
    execution_start_time = time.perf_counter()
    tags = {"workflow": extra_data.get("workflow", "unknown")}

    def _fail(error={}):
        cache.set_unused.remote(prompt)
        handle_execution_error(messages, client_id, prompt_id, prompt, staged, error)
        REQUEST_COUNT.inc(1, {**tags, "status": "fail"})

    try:
        sigs, outputs = ray.get(cache.get.remote(prompt))
        messages.add.remote(
            "execution_cached",
            {"nodes": list(outputs.keys()), "prompt_id": prompt_id},
            client_id,
            broadcast=False
        )

        staged = set()
        stage_list = StageList(prompt)
        for node_id in execute_outputs:
            stage_list.add_node(node_id)

        while not stage_list.is_empty():
            node_id, error, exc = stage_list.stage_node_execution()
            if error:
                raise RuntimeError((error, exc))

            result, error, exc = execute_node(
                client_id,
                prompt_id,
                node_id,
                prompt,
                sigs,
                outputs,
                staged,
                gpu_pool,
                messages,
                extra_data,
            )
            if result != ExecutionResult.STAGED:
                raise RuntimeError((error, exc))

            stage_list.complete_node_staging()

        cache.set.remote(outputs, prompt)

        for refs in outputs.values():
            for ref in refs:
                ray.get(ref)

    except RuntimeError as re:
        err, _ = re.args[0]
        _fail(err)
        return

    except Exception as _:
        _fail()
        return

    else:
        cache.set_unused.remote(prompt)
        REQUEST_COUNT.inc(1, {**tags, "status": "ok"})
        REQUEST_LATENCY.observe(time.perf_counter() - extra_data.get("request_start_time", time.perf_counter()), tags)
        EXECUTION_LATENCY.observe(time.perf_counter() - execution_start_time, tags)


def handle_execution_error(messages, client_id, prompt_id, prompt, staged, error):
    mes = {
            "prompt_id": prompt_id,
            "prompt": prompt,
            "staged": list(staged),
            **error,
    }
    messages.add.remote("execution_failed", mes, client_id, broadcast=False)

def validate_inputs(prompt, item, validated):
    unique_id = item
    if unique_id in validated:
        return validated[unique_id]

    inputs = prompt[unique_id]['inputs']
    class_type = prompt[unique_id]['class_type']
    obj_class = nodes.NODE_CLASS_MAPPINGS[class_type]

    class_inputs = obj_class.INPUT_TYPES()
    valid_inputs = set(class_inputs.get('required',{})).union(set(class_inputs.get('optional',{})))

    errors = []
    valid = True

    validate_function_inputs = []
    validate_has_kwargs = False
    if hasattr(obj_class, "VALIDATE_INPUTS"):
        argspec = inspect.getfullargspec(obj_class.VALIDATE_INPUTS)
        validate_function_inputs = argspec.args
        validate_has_kwargs = argspec.varkw is not None
    received_types = {}

    for x in valid_inputs:
        input_type, input_category, extra_info = get_input_info(obj_class, x, class_inputs)
        assert extra_info is not None
        if x not in inputs:
            if input_category == "required":
                error = {
                    "type": "required_input_missing",
                    "message": "Required input is missing",
                    "details": f"{x}",
                    "extra_info": {
                        "input_name": x
                    }
                }
                errors.append(error)
            continue

        val = inputs[x]
        info = (input_type, extra_info)
        if isinstance(val, list):
            if len(val) != 2:
                error = {
                    "type": "bad_linked_input",
                    "message": "Bad linked input, must be a length-2 list of [node_id, slot_index]",
                    "details": f"{x}",
                    "extra_info": {
                        "input_name": x,
                        "input_config": info,
                        "received_value": val
                    }
                }
                errors.append(error)
                continue

            o_id = val[0]
            o_class_type = prompt[o_id]['class_type']
            r = nodes.NODE_CLASS_MAPPINGS[o_class_type].RETURN_TYPES
            received_type = r[val[1]]
            received_types[x] = received_type
            if 'input_types' not in validate_function_inputs and not validate_node_input(received_type, input_type):
                details = f"{x}, received_type({received_type}) mismatch input_type({input_type})"
                error = {
                    "type": "return_type_mismatch",
                    "message": "Return type mismatch between linked nodes",
                    "details": details,
                    "extra_info": {
                        "input_name": x,
                        "input_config": info,
                        "received_type": received_type,
                        "linked_node": val
                    }
                }
                errors.append(error)
                continue
            try:
                r = validate_inputs(prompt, o_id, validated)
                if r[0] is False:
                    # `r` will be set in `validated[o_id]` already
                    valid = False
                    continue
            except Exception as ex:
                typ, _, tb = sys.exc_info()
                valid = False
                exception_type = full_type_name(typ)
                reasons = [{
                    "type": "exception_during_inner_validation",
                    "message": "Exception when validating inner node",
                    "details": str(ex),
                    "extra_info": {
                        "input_name": x,
                        "input_config": info,
                        "exception_message": str(ex),
                        "exception_type": exception_type,
                        "traceback": traceback.format_tb(tb),
                        "linked_node": val
                    }
                }]
                validated[o_id] = (False, reasons, o_id)
                continue
        else:
            try:
                # Unwraps values wrapped in __value__ key. This is used to pass
                # list widget value to execution, as by default list value is
                # reserved to represent the connection between nodes.
                if isinstance(val, dict) and "__value__" in val:
                    val = val["__value__"]
                    inputs[x] = val

                if input_type == "INT":
                    val = int(val)
                    inputs[x] = val
                if input_type == "FLOAT":
                    val = float(val)
                    inputs[x] = val
                if input_type == "STRING":
                    val = str(val)
                    inputs[x] = val
                if input_type == "BOOLEAN":
                    val = bool(val)
                    inputs[x] = val
            except Exception as ex:
                error = {
                    "type": "invalid_input_type",
                    "message": f"Failed to convert an input value to a {input_type} value",
                    "details": f"{x}, {val}, {ex}",
                    "extra_info": {
                        "input_name": x,
                        "input_config": info,
                        "received_value": val,
                        "exception_message": str(ex)
                    }
                }
                errors.append(error)
                continue

            if x not in validate_function_inputs and not validate_has_kwargs:
                if "min" in extra_info and val < extra_info["min"]:
                    error = {
                        "type": "value_smaller_than_min",
                        "message": "Value {} smaller than min of {}".format(val, extra_info["min"]),
                        "details": f"{x}",
                        "extra_info": {
                            "input_name": x,
                            "input_config": info,
                            "received_value": val,
                        }
                    }
                    errors.append(error)
                    continue
                if "max" in extra_info and val > extra_info["max"]:
                    error = {
                        "type": "value_bigger_than_max",
                        "message": "Value {} bigger than max of {}".format(val, extra_info["max"]),
                        "details": f"{x}",
                        "extra_info": {
                            "input_name": x,
                            "input_config": info,
                            "received_value": val,
                        }
                    }
                    errors.append(error)
                    continue

                if isinstance(input_type, list):
                    combo_options = input_type
                    if val not in combo_options:
                        input_config = info
                        list_info = ""

                        # Don't send back gigantic lists like if they're lots of
                        # scanned model filepaths
                        if len(combo_options) > 20:
                            list_info = f"(list of length {len(combo_options)})"
                            input_config = None
                        else:
                            list_info = str(combo_options)

                        error = {
                            "type": "value_not_in_list",
                            "message": "Value not in list",
                            "details": f"{x}: '{val}' not in {list_info}",
                            "extra_info": {
                                "input_name": x,
                                "input_config": input_config,
                                "received_value": val,
                            }
                        }
                        errors.append(error)
                        continue

    if len(validate_function_inputs) > 0 or validate_has_kwargs:
        input_data_all, _ = get_input_data(inputs, obj_class, unique_id)
        input_filtered = {}
        for x in input_data_all:
            if x in validate_function_inputs or validate_has_kwargs:
                input_filtered[x] = input_data_all[x]
        if 'input_types' in validate_function_inputs:
            input_filtered['input_types'] = [received_types]

        #ret = obj_class.VALIDATE_INPUTS(**input_filtered)
        ret = _map_node_over_list(obj_class, "VALIDATE_INPUTS", input_filtered)
        for x in input_filtered:
            for i, r in enumerate(ret):
                if r is not True and not isinstance(r, ExecutionBlocker):
                    details = f"{x}"
                    if r is not False:
                        details += f" - {str(r)}"

                    error = {
                        "type": "custom_validation_failed",
                        "message": "Custom validation failed for node",
                        "details": details,
                        "extra_info": {
                            "input_name": x,
                        }
                    }
                    errors.append(error)
                    continue

    if len(errors) > 0 or valid is not True:
        ret = (False, errors, unique_id)
    else:
        ret = (True, [], unique_id)

    validated[unique_id] = ret
    return ret

def full_type_name(klass):
    module = klass.__module__
    if module == 'builtins':
        return klass.__qualname__
    return module + '.' + klass.__qualname__

def validate_prompt(prompt):
    outputs = set()
    for x in prompt:
        if 'class_type' not in prompt[x]:
            error = {
                "type": "invalid_prompt",
                "message": "Cannot execute because a node is missing the class_type property.",
                "details": f"Node ID '#{x}'",
                "extra_info": {}
            }
            return (False, error, [], {})

        class_type = prompt[x]['class_type']
        class_ = nodes.NODE_CLASS_MAPPINGS.get(class_type, None)
        if class_ is None:
            error = {
                "type": "invalid_prompt",
                "message": f"Cannot execute because node {class_type} does not exist.",
                "details": f"Node ID '#{x}'",
                "extra_info": {}
            }
            return (False, error, [], {})

        if hasattr(class_, 'OUTPUT_NODE') and class_.OUTPUT_NODE is True:
            outputs.add(x)

    if len(outputs) == 0:
        error = {
            "type": "prompt_no_outputs",
            "message": "Prompt has no outputs",
            "details": "",
            "extra_info": {}
        }
        return (False, error, [], {})

    good_outputs = set()
    errors = []
    node_errors = {}
    validated = {}
    for o in outputs:
        valid = False
        reasons = []
        try:
            m = validate_inputs(prompt, o, validated)
            valid = m[0]
            reasons = m[1]
        except Exception as ex:
            typ, _, tb = sys.exc_info()
            valid = False
            exception_type = full_type_name(typ)
            reasons = [{
                "type": "exception_during_validation",
                "message": "Exception when validating node",
                "details": str(ex),
                "extra_info": {
                    "exception_type": exception_type,
                    "traceback": traceback.format_tb(tb)
                }
            }]
            validated[o] = (False, reasons, o)

        if valid is True:
            good_outputs.add(o)
        else:
            logging.error(f"Failed to validate prompt for output {o}:")
            if len(reasons) > 0:
                logging.error("* (prompt):")
                for reason in reasons:
                    logging.error(f"  - {reason['message']}: {reason['details']}")
            errors += [(o, reasons)]
            for node_id, result in validated.items():
                valid = result[0]
                reasons = result[1]
                # If a node upstream has errors, the nodes downstream will also
                # be reported as invalid, but there will be no errors attached.
                # So don't return those nodes as having errors in the response.
                if valid is not True and len(reasons) > 0:
                    if node_id not in node_errors:
                        class_type = prompt[node_id]['class_type']
                        node_errors[node_id] = {
                            "errors": reasons,
                            "dependent_outputs": [],
                            "class_type": class_type
                        }
                        logging.error(f"* {class_type} {node_id}:")
                        for reason in reasons:
                            logging.error(f"  - {reason['message']}: {reason['details']}")
                    node_errors[node_id]["dependent_outputs"].append(o)
            logging.error("Output will be ignored")

    if len(good_outputs) == 0:
        errors_list = []
        for o, errors in errors:
            for error in errors:
                errors_list.append(f"{error['message']}: {error['details']}")
        errors_list = "\n".join(errors_list)

        error = {
            "type": "prompt_outputs_failed_validation",
            "message": "Prompt outputs failed validation",
            "details": errors_list,
            "extra_info": {}
        }

        return (False, error, list(good_outputs), node_errors)

    return (True, None, list(good_outputs), node_errors)
