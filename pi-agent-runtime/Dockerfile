FROM node:22-bookworm-slim AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:22-bookworm-slim AS runtime

ENV NODE_ENV=production \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PI_AGENT_HOST=0.0.0.0 \
    PI_AGENT_PORT=3210 \
    PI_AGENT_STATE_ROOT=/var/lib/pi-agent \
    PI_AGENT_TASK_ENVIRONMENT_ROOT=/var/lib/pi-agent-task \
    PI_AGENT_WORKSPACE_ROOT=/workspaces \
    PI_AGENT_SKILLS_ROOT=/opt/pi-agent/skills \
    PI_AGENT_ENVIRONMENT_TEMPLATE_ROOT=/opt/pi-agent/environment-template \
    PI_AGENT_TASK_UID_MIN=20000 \
    PI_AGENT_TASK_UID_MAX=60000 \
    PI_AGENT_TASK_GID=65532 \
    PI_AGENT_WORKSPACE_OWNER_UID=1000 \
    PI_AGENT_WORKSPACE_OWNER_GID=1000 \
    PI_CODING_AGENT_DIR=/var/lib/pi-agent/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/pi-agent /var/lib/pi-agent-task /workspaces /opt/pi-agent/skills /opt/pi-agent/environment-template \
    && chmod 700 /var/lib/pi-agent \
    && chmod 755 /var/lib/pi-agent-task /workspaces /opt/pi-agent/skills

WORKDIR /app
COPY --from=build --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/dist ./dist
COPY --chown=node:node skills /opt/pi-agent/skills
COPY environment-template /opt/pi-agent/environment-template

USER root
EXPOSE 3210

CMD ["node", "dist/server.js"]
