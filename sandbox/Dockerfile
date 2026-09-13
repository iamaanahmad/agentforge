FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git gcc libc6-dev && rm -rf /var/lib/apt/lists/*
# Dependencies are installed offline during generated jobs. Owners can build reviewed wheelhouses into this image.
RUN pip download --dest /opt/wheels setuptools wheel ruff==0.16.7 && pip install --no-index --find-links=/opt/wheels setuptools wheel ruff==0.16.7
COPY sandbox/run.py sandbox/image.py /opt/a4g/
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/bin/sleep"]
CMD ["120"]
