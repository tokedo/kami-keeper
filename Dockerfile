# kami-keeper — lab image (SPEC D4).
#
# Built from a `git archive` tarball of the manifest SHA that
# carries KEEPER_PIN.txt. The build context must hold EXACTLY ONE
# artifact (`kami-keeper-<sha>.tar.gz`); two refuse. The archive's
# KEEPER_PIN.txt must equal the `KEEPER_PIN` build arg, or the build
# refuses. No build artifact is tracked in the repo (.gitignore: *.tgz,
# *.tar.gz, dist/).
#
#   git archive --format=tar.gz --prefix=kami-keeper/ -o kami-keeper-$SHA.tar.gz $SHA
#   docker build --build-arg KEEPER_PIN=$SHA -t kami-keeper:$SHA .
#
# Python 3.11 (exact patch tag), the interpreter of the systemd deployment
# (SPEC D4); dependencies are hash-pinned (requirements.txt).
FROM python:3.11.13-slim-bookworm

ARG KEEPER_PIN
RUN test -n "$KEEPER_PIN" || (echo "KEEPER_PIN build arg is required" >&2; exit 1)

WORKDIR /build
COPY . /build/context/
# exactly one artifact, and its pin must match
RUN set -eu; \
    n=$(ls /build/context/kami-keeper-*.tar.gz 2>/dev/null | wc -l); \
    if [ "$n" -ne 1 ]; then echo "refusing: expected exactly one kami-keeper-<sha>.tar.gz artifact, found $n" >&2; exit 1; fi; \
    tar -xzf /build/context/kami-keeper-*.tar.gz -C /build; \
    pin=$(cat /build/kami-keeper/KEEPER_PIN.txt); \
    if [ "$pin" != "$KEEPER_PIN" ]; then echo "refusing: KEEPER_PIN.txt=$pin != build arg $KEEPER_PIN" >&2; exit 1; fi; \
    rm -rf /build/context

WORKDIR /app
RUN cp -r /build/kami-keeper/. /app/ && rm -rf /build \
    && pip install --no-cache-dir --require-hashes -r requirements.txt \
    && echo "$KEEPER_PIN" > /app/KEEPER_PIN.txt

ENV KAMI_KEEPER_PROFILE=lab \
    KAMI_KEEPER_DATA_DIR=/data \
    KAMI_KEEPER_SOCKET=/data/keeper.sock \
    KAMI_KEEPER_LENS_SOCKET=/srv/lens/kami-lens.sock \
    KAMI_KEEPER_KEYS_FILE=/srv/keys/.env \
    KAMI_KEEPER_SECRETS_BACKEND=env

VOLUME ["/data", "/srv/lens", "/srv/keys"]
# `--network host`: the egress lockdown lives in the host's OUTPUT chain.
ENTRYPOINT ["python", "-m", "keeper", "daemon"]
