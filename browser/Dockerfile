FROM python:3.13-slim
RUN pip install --no-cache-dir playwright==1.58.0 && playwright install --with-deps chromium
RUN chmod -R a+rX /root/.cache/ms-playwright && mv /root/.cache/ms-playwright /opt/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY browser/runner.py /opt/a4g/browser_runner.py
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/a4g/browser_runner.py"]
