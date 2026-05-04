# Gilbert browser-plugin runtime image.
#
# Microsoft's base ``mcr.microsoft.com/playwright:vX.Y.Z-jammy`` ships
# Node + Chromium + the OS shared libs, but doesn't keep the JS
# Playwright package itself installed (it's used during image build to
# fetch browsers, then discarded). We layer it on top so
# ``require('playwright')`` resolves cleanly inside the container.
#
# Built once per ``PW_VERSION`` by ``BrowserContainer.start()`` if the
# resulting tag (``gilbert-browser:v<PW_VERSION>``) isn't already in
# the local Docker image cache. Subsequent starts reuse the built
# image — no network, no npm install.

ARG PW_VERSION

FROM mcr.microsoft.com/playwright:v${PW_VERSION}-jammy

ARG PW_VERSION
WORKDIR /app
RUN npm init -y >/dev/null \
 && npm install --no-audit --no-fund --no-package-lock playwright@${PW_VERSION}

# Default command is overridden by ``docker run`` with the launcher
# script — this just makes the image self-documenting.
CMD ["node", "-e", "console.log('gilbert-browser ready'); setInterval(()=>{},1<<30);"]
