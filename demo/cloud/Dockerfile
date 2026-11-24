# EdgeFHE demo cloud image (the PC acting as the cloud): the unchanged efnet_server, built from the staged overlay of
# v8b_rev5 (v8b_rev5 plus the CDC catalogue line and model JSON, demo/overlay/make_overlay.py), the demo-only TLS
# key-log shim, the server launcher and the loopback rehearsal. Same toolchain as the board: Ubuntu 22.04, g++-11 and
# OpenSSL 3.0. No APP_BUILD, hence no efnet_client and no OpenFHE: the cloud holds no key of any kind.
#
# Build from the repository root; demo/cloud/Dockerfile.dockerignore limits the context to v8b_rev5/ and demo/:
#   docker build --progress=plain -f demo/cloud/Dockerfile --build-arg EDGEFHE_COMMIT=<commit> -t edgefhe-demo-cloud:<commit> .
# Use: sh /opt/efhe/run_server.sh [--keylog] [--loopback] [--connections N]   (the cloud server, see that file)
#      bash /opt/efhe/rehearse_loopback.sh                                    (key-log rehearsal, see that file)
#      bash /opt/efhe/make_demo_pki.sh all-in-one | ca-and-server | sign-client ...
# Layout: /repo (copy of the build context), /src (staged overlay tree), /src.OVERLAY_RECORD.json, /build (CMake build
#   of the net targets), /opt/efhe (libefhe_keylog.so, the three scripts, OVERLAY_RECORD.json, BUILD_SELFTEST.txt and
#   BUILD_SHA256, the build record that run_server.sh checks before every start).
FROM ubuntu:22.04
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential g++-11 cmake libssl-dev openssl python3 tcpdump \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV LANG=C.UTF-8

ARG EDGEFHE_COMMIT=dev
COPY v8b_rev5 /repo/v8b_rev5
COPY demo /repo/demo

# Staged overlay outside the repository copy: make_overlay.py verifies the base tree, the staged tree (282 files) and
# every hash in demo/model/EXPECTED.json, and writes /src.OVERLAY_RECORD.json with the staged_tree_id.
RUN python3 -B /repo/demo/overlay/make_overlay.py --repo /repo --out /src --commit "$EDGEFHE_COMMIT"

# The net targets only (build_and_selftest.sh:20-21 without APP_BUILD): efnet_server, libefnet_cloud.so, efnet_probe and
# the self-tests.
RUN cmake -S /src/software/client -B /build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=g++-11 \
 && cmake --build /build -j "$(nproc)"

# The three self-test markers of build_and_selftest.sh:26-28. efnet_server must load the system OpenSSL 3 dynamically and
# import SSL_new from it; otherwise LD_PRELOAD could not interpose the key-log hook.
RUN for t in span permit wire; do /build/efnet_${t}_selftest | tee /build/${t}.log; done \
 && grep -qF 'CPP_MTLS_SPAN_BYTE_HASH_EQUIVALENCE_PASS bytes=51904512 refused=33' /build/span.log \
 && grep -qF 'CPP_MTLS_INVOCATION_PERMIT_PASS positive=1 refused=14' /build/permit.log \
 && grep -qF 'CPP_E2E_WIRE_AND_PADDED_AFFINE_PASS exact_checks=1572899 refusals=78' /build/wire.log \
 && ldd /build/efnet_server | tee /build/efnet_server.ldd \
 && grep -qE '^[[:space:]]*libssl\.so\.3 => /' /build/efnet_server.ldd \
 && nm -D --undefined-only /build/efnet_server >/build/efnet_server.imports \
 && grep -qw SSL_new /build/efnet_server.imports \
 && install -d -m 0755 /opt/efhe \
 && cat /build/span.log /build/permit.log /build/wire.log >/opt/efhe/BUILD_SELFTEST.txt \
 && echo 'EDGEFHE_CLOUD_SELFTESTS_PASS span permit wire; efnet_server imports SSL_new from libssl.so.3'

# The demo-only key-log shim and its build-time checks: inert without EFHE_DEMO_KEYLOG; with it, a new 0600 file and the
# warning line; an existing file, a symbolic link or a relative path stops the process with status 3 before main.
RUN gcc -O2 -Wall -Wextra -Werror -shared -fPIC -o /opt/efhe/libefhe_keylog.so /repo/demo/cloud/efhe_keylog_preload.c \
      -ldl -lssl -lcrypto \
 && nm -D --defined-only /opt/efhe/libefhe_keylog.so >/tmp/shim.symbols \
 && grep -qw SSL_new /tmp/shim.symbols \
 && shim=/opt/efhe/libefhe_keylog.so && t=$(mktemp -d) \
 && run() { local rc=0; env "$@" /bin/true >"$t/out" 2>&1 || rc=$?; echo "$rc"; } \
 && [ "$(run -u EFHE_DEMO_KEYLOG LD_PRELOAD="$shim")" = 0 ] && [ ! -s "$t/out" ] \
 && [ "$(run LD_PRELOAD="$shim" EFHE_DEMO_KEYLOG="$t/k.log")" = 0 ] \
 && grep -q "^WARNING_DEMO_TLS_KEYLOG_ENABLED path=$t/k.log " "$t/out" \
 && [ "$(stat -c %a "$t/k.log")" = 600 ] && [ ! -s "$t/k.log" ] \
 && [ "$(run LD_PRELOAD="$shim" EFHE_DEMO_KEYLOG="$t/k.log")" = 3 ] \
 && grep -q '^STOP_DEMO_TLS_KEYLOG_OPEN errno=17 ' "$t/out" \
 && ln -s missing.log "$t/link.log" && [ "$(run LD_PRELOAD="$shim" EFHE_DEMO_KEYLOG="$t/link.log")" = 3 ] \
 && [ ! -e "$t/missing.log" ] \
 && [ "$(run LD_PRELOAD="$shim" EFHE_DEMO_KEYLOG=relative.log)" = 3 ] && [ ! -e relative.log ] \
 && rm -rf "$t" \
 && echo 'EDGEFHE_KEYLOG_SHIM_BUILD_CHECKS_PASS inert create_0600 refuse_existing refuse_symlink refuse_relative' \
    | tee -a /opt/efhe/BUILD_SELFTEST.txt

# The launcher, the rehearsal and the demo PKI script (all LF-only), the overlay record and the build record.
RUN for f in /repo/demo/cloud/run_server.sh /repo/demo/cloud/rehearse_loopback.sh /repo/demo/pki/make_demo_pki.sh; do \
      if grep -q $'\r' "$f"; then echo "CR bytes in $f: shell scripts must be LF-only"; exit 1; fi; \
      install -m 0755 "$f" /opt/efhe/; \
    done \
 && sh -n /opt/efhe/run_server.sh && bash -n /opt/efhe/rehearse_loopback.sh && bash -n /opt/efhe/make_demo_pki.sh \
 && install -m 0644 /src.OVERLAY_RECORD.json /opt/efhe/OVERLAY_RECORD.json \
 && tree_id=$(python3 -B -c 'import json; print(json.load(open("/opt/efhe/OVERLAY_RECORD.json"))["staged_tree_id"])') \
 && [[ $tree_id =~ ^[0-9a-f]{64}$ ]] \
 && libssl=$(readlink -f "$(sed -n 's/^[[:space:]]*libssl\.so\.3 => \(\/[^ ]*\) .*/\1/p' /build/efnet_server.ldd)") \
 && libcrypto=$(readlink -f "$(sed -n 's/^[[:space:]]*libcrypto\.so\.3 => \(\/[^ ]*\) .*/\1/p' /build/efnet_server.ldd)") \
 && { echo '# EdgeFHE demo cloud image: build record (sha256sum -c format; lines starting with # are comments)'; \
      echo "# edgefhe_commit=$EDGEFHE_COMMIT"; \
      echo "# staged_tree_id=$tree_id"; \
      echo "# toolchain: $(g++-11 --version | sed -n 1p); $(cmake --version | sed -n 1p); $(openssl version); $(ldd --version | sed -n 1p)"; \
      sha256sum /build/efnet_server /build/libefnet_cloud.so /build/efnet_probe /build/efnet_span_selftest \
        /build/efnet_permit_selftest /build/efnet_wire_selftest /build/efnet_wire_fixture "$libssl" "$libcrypto" \
        /opt/efhe/libefhe_keylog.so /opt/efhe/run_server.sh /opt/efhe/rehearse_loopback.sh /opt/efhe/make_demo_pki.sh \
        /opt/efhe/OVERLAY_RECORD.json /opt/efhe/BUILD_SELFTEST.txt; \
    } >/opt/efhe/BUILD_SHA256 \
 && (cd / && sha256sum --check --strict --quiet /opt/efhe/BUILD_SHA256) \
 && cat /opt/efhe/BUILD_SHA256

LABEL org.opencontainers.image.title="edgefhe-demo-cloud" \
      org.opencontainers.image.description="EdgeFHE demo cloud: unchanged efnet_server (ciphertext only, no key of any kind) and a demo-only TLS key-log shim" \
      org.opencontainers.image.revision="$EDGEFHE_COMMIT"
EXPOSE 18443/tcp
CMD ["sh", "/opt/efhe/run_server.sh"]
