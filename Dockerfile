# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit: the selector reads the original files to apply each patch.
# Nothing else is downloaded: the selector uses the standard library only.
COPY fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py && rm /tmp/fetch_sqlglot.py

COPY selector.py map.bin /app/
RUN chmod 755 /app && chmod 644 /app/selector.py /app/map.bin
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-S", "/app/selector.py"]
