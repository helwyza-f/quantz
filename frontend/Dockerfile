FROM node:22-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json ./
COPY frontend/package.json frontend/package.json
RUN npm ci --workspace @quantz/frontend

FROM deps AS builder

ARG NEXT_PUBLIC_API_BASE=http://127.0.0.1:8787
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE

COPY frontend ./frontend
WORKDIR /app/frontend
RUN npm run build

FROM node:22-alpine AS runner

ENV NODE_ENV=production
WORKDIR /app

COPY package.json package-lock.json ./
COPY frontend/package.json frontend/package.json
RUN npm ci --workspace @quantz/frontend --omit=dev

COPY --from=builder /app/frontend/.next frontend/.next
COPY --from=builder /app/frontend/next.config.ts frontend/next.config.ts
COPY --from=builder /app/frontend/public frontend/public

WORKDIR /app/frontend
EXPOSE 3000

CMD ["npx", "next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
