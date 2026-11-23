FROM python:3.12-slim

WORKDIR /srv
COPY common/ /srv/common/
COPY prober/requirements.txt /srv/prober/requirements.txt
RUN pip install --no-cache-dir -r /srv/prober/requirements.txt
COPY prober/ /srv/prober/

ENV PYTHONPATH=/srv \
    RECEIVER_HOST=receiver \
    RECEIVER_PORT=5055

EXPOSE 8000
CMD ["uvicorn", "prober.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
