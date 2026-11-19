FROM alpine:3.23.3
RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apk/repositories \
    && apk add --no-cache iperf3 curl bind-tools iproute2 tcpdump socat python3
CMD ["sleep", "infinity"]
