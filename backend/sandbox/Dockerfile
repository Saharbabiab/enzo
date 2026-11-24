# Enzo's code sandbox (Hermes plan H5): runs Python snippets with no internet and tight limits.
# Built and run next to Enzo by sandbox/compose.example.yml. Standard library only: nothing to install.
FROM python:3.11-slim
RUN useradd --create-home --uid 10001 runner
COPY runner.py /opt/sandbox/runner.py
USER runner
WORKDIR /tmp
EXPOSE 8800
CMD ["python", "-I", "/opt/sandbox/runner.py"]
