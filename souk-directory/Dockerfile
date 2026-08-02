# Multi-stage: compile TypeScript, then serve the static output with
# nothing but Python's stdlib http.server — no npm/node in the runtime
# image, keeping the served container as small/boring as the rest of this
# repo's Python-first tooling.
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json tsconfig.json ./
RUN npm install
COPY src ./src
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY index.html agent.html style.css ./
COPY --from=build /app/dist ./dist
EXPOSE 8080
CMD ["python", "-m", "http.server", "8080"]
