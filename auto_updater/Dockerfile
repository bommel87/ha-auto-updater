ARG BUILD_FROM
FROM $BUILD_FROM

COPY run.py /run.py

# Ohne s6-Init starten, damit SUPERVISOR_TOKEN sicher in der Umgebung ankommt.
ENTRYPOINT ["python3", "-u", "/run.py"]
CMD []
