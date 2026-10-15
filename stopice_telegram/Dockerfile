FROM python:3.13-alpine
RUN apk add --no-cache tzdata
COPY main.py web.py index.html CHANGELOG.md /
CMD ["python3", "-u", "/main.py"]
