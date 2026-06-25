# PDF Accessibility Remediation Engine
# Single stateless container: Python 3.12 (FastAPI) + headless JRE + veraPDF.
#
# veraPDF needs a JRE; OpenDataLoader (Phase 2) needs Java 11+ and Python 3.10+.
# The python:3.12-slim base + default-jre-headless satisfies all three.
#
# Build (from the repo root, so the backend/ context is in scope):
#   docker build -f docker/Dockerfile -t remediation-engine .
# Run:
#   docker run --rm -p 8000:8000 remediation-engine
# Smoke-test veraPDF inside the image (no server needed):
#   docker run --rm remediation-engine /opt/verapdf/verapdf --version

FROM python:3.12-slim

# veraPDF release to install. greenfield = the open-source veraPDF build.
ARG VERAPDF_VERSION=1.26.2
# The minor-version folder on the download server (e.g. 1.26 for 1.26.2).
ARG VERAPDF_MINOR=1.26
# Temurin 11 JRE. The veraPDF 1.26 IzPack installer throws
# ArrayIndexOutOfBoundsException under JDK 17+, so we pin Java 11 for both the
# install step and the runtime (veraPDF and OpenDataLoader both support 11+).
ARG JRE_VERSION=11.0.24_8
ARG JRE_TAG=jdk-11.0.24%2B8

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JAVA_HOME=/opt/jre11 \
    VERAPDF_HOME=/opt/verapdf \
    VERAPDF_PATH=/opt/verapdf/verapdf
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# --- Fetch tools ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
        fontconfig \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# --- Temurin 11 JRE (headless) from Adoptium ---
RUN set -eux; \
    curl -fsSL \
      "https://github.com/adoptium/temurin11-binaries/releases/download/${JRE_TAG}/OpenJDK11U-jre_x64_linux_hotspot_${JRE_VERSION}.tar.gz" \
      -o /tmp/jre.tgz; \
    mkdir -p /opt/jre11; \
    tar xzf /tmp/jre.tgz -C /opt/jre11 --strip-components=1; \
    rm /tmp/jre.tgz; \
    java -version

# --- Install veraPDF headlessly via its IzPack auto-install descriptor ---
# The installer is a GUI app that also supports unattended installs given an XML
# answer file (docker/verapdf-auto-install.xml). It selects the default packs and
# installs the *nix CLI scripts to $VERAPDF_HOME.
COPY docker/verapdf-auto-install.xml /tmp/verapdf-auto-install.xml
RUN set -eux; \
    curl -fsSL \
      "https://software.verapdf.org/releases/${VERAPDF_MINOR}/verapdf-greenfield-${VERAPDF_VERSION}-installer.zip" \
      -o /tmp/verapdf-installer.zip; \
    unzip -q /tmp/verapdf-installer.zip -d /tmp/verapdf-installer; \
    java -jar "/tmp/verapdf-installer/verapdf-greenfield-${VERAPDF_VERSION}/verapdf-izpack-installer-${VERAPDF_VERSION}.jar" \
      /tmp/verapdf-auto-install.xml; \
    ln -sf /opt/verapdf/verapdf /usr/local/bin/verapdf; \
    rm -rf /tmp/verapdf-installer /tmp/verapdf-installer.zip /tmp/verapdf-auto-install.xml; \
    /opt/verapdf/verapdf --version

# --- Python deps ---
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- Application code ---
COPY backend/app /app/app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Container-level health check hits the dependency probe (proves JRE+veraPDF).
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
