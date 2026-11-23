FROM python:3.12-slim

WORKDIR /srv
COPY common/ /srv/common/
COPY receiver/receiver.py /srv/receiver/receiver.py

ENV PYTHONPATH=/srv \
    PORT=5055

EXPOSE 5055/udp
CMD ["python", "receiver/receiver.py"]
