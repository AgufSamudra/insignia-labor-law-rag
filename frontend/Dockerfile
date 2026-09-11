FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
# Keep the image build independent of npm's audit/update services and tolerate
# short-lived registry/proxy interruptions during a clean Docker build.
RUN npm ci \
    --no-audit \
    --no-fund \
    --prefer-offline \
    --fetch-retries=5 \
    --fetch-retry-factor=2 \
    --fetch-retry-mintimeout=1000 \
    --fetch-retry-maxtimeout=20000 \
    --fetch-timeout=120000

COPY . .

# An empty value makes the browser use same-origin paths through Nginx.
ARG VITE_API_URL=""
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM nginx:1.28-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
