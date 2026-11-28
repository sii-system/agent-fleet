FROM ubuntu:24.04
USER root
WORKDIR /app
COPY input.txt /app/input.txt
RUN test "$(cat /app/input.txt)" = native-e2b
