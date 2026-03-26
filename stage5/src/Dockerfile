FROM docker:24-dind

ENV DEBIAN_FRONTEND=noninteractive

# Install networking tools
RUN apk add --no-cache iproute2 iptables iputils procps sed bash

WORKDIR /root

# Copy scripts
COPY setup.sh /usr/local/bin/setup.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/setup.sh /usr/local/bin/entrypoint.sh

EXPOSE 2375

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
