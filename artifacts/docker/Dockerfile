# Default base dockerfile for pytorch distributed jobs
FROM pytorch/pytorch:1.9.1-cuda11.1-cudnn8-runtime

WORKDIR /app
COPY requirements.txt /app
RUN pip install -r requirements.txt

COPY . /app
ENTRYPOINT ["torchrun"]