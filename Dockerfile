FROM pytorch/pytorch:2.4.0-cuda11.8-cudnn9-runtime

WORKDIR /ComfyScale

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]
