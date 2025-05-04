FROM pytorch/pytorch:2.4.0-cuda11.8-cudnn9-runtime

WORKDIR /ComfyScale

RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY custom_nodes/*/requirements.txt ./custom_nodes_reqs/
RUN for req in custom_nodes_reqs/*.txt; do pip install -r "$req"; done

COPY . .

CMD ["python", "main.py"]
