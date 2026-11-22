# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The packages the commits were labeled with, so tests can be run at decision time.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock && rm /tmp/requirements.lock

# sqlglot at the pinned commit: the selector reads the original files to apply each patch.
COPY fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py && rm /tmp/fetch_sqlglot.py \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests > /dev/null; \
    find /opt/sqlglot -type d -exec chmod 755 {} + && find /opt/sqlglot -type f -exec chmod 644 {} +

COPY selector.py map.bin /app/
RUN chmod 755 /app && chmod 644 /app/selector.py /app/map.bin
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
CMD ["python", "-S", "/app/selector.py"]
