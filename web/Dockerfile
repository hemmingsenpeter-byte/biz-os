# Build from the repository root: docker build -f web/Dockerfile -t biz-os-web .
FROM node:24-bookworm-slim AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app/web
# Package stores stay cached when source files change. Installing and building in
# one layer also keeps the temporary image small on copy-based storage drivers.
COPY web/ ./
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=cache,target=/pnpm/store \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm install --global pnpm@11.25.0 \
    && pnpm install --frozen-lockfile --store-dir /pnpm/store \
    && mkdir -p public \
    && pnpm build \
    && cp -r public .next/standalone/ \
    && cp -r .next/static .next/standalone/.next/

FROM node:24-bookworm-slim
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
WORKDIR /app
COPY --from=build --chown=node:node /app/web/.next/standalone ./
USER node
EXPOSE 3000
CMD ["node", "server.js"]
