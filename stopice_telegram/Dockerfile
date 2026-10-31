FROM python:3.13-alpine
RUN apk add --no-cache tzdata
COPY main.py web.py ha_map.py geo_alerts.py index.html view.html CHANGELOG.md /
CMD ["python3", "-u", "/main.py"]
