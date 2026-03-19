FROM apache/airflow:2.7.0-python3.10

# Install additional requirements using Airflow constraints for speed/repro
COPY requirements.txt /
ARG AIRFLOW_VERSION=2.7.0
ARG PYTHON_VERSION=3.10
ENV AIRFLOW_CONSTRAINTS_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
RUN pip install --no-cache-dir --constraint "${AIRFLOW_CONSTRAINTS_URL}" -r /requirements.txt

USER airflow
