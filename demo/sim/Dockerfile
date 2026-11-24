# EdgeFHE demo PC rehearsal image (edgefhe-demo-sim): one whole demo session on the PC, with the PL bit-exact software
# model (efnet_client --backend spec) in place of the KV260 FPGA. The PC plays the owner (gateway and owner screen), the
# wire (loopback) and the cloud (the unchanged efnet_server). Rehearsal only: x86 binaries, timings and energy never
# stand in for the board (v8b_rev5/docs/DEPENDENCIES_zh-TW.md:31).
#
# Stages:
#   toolchain  ubuntu:22.04 with the packages of DEPENDENCIES_zh-TW.md:9 (plus tcpdump, ca-certificates for the clone
#              and iproute2 for the gateway preflight) and OpenFHE 1.5.1 built exactly as DEPENDENCIES_zh-TW.md:16-27
#              into /root/.local/openfhe-v1.5.1
#   sources    v8b_rev5/ and demo/ of the build context with caches and local artefacts dropped, so that neither a
#              stray __pycache__ nor a key, log or capture under demo/ can reach the image (no dockerignore needed:
#              BuildKit sends only the two COPY sources)
#   sim        the staged overlay (demo/overlay/make_overlay.py -> /src), the full V8b build and self-tests
#              (/src/tools/build_and_selftest.sh -> /out), the demo-only key-log shim and the launcher of demo/cloud,
#              the gateway (demo/edge), the owner screen (demo/ui) and the rehearsal scripts (demo/sim)
#
# Build from the repository root (once: about 3 minutes with 24 CPUs, up to 30-40 minutes on a small PC; later builds
# reuse the OpenFHE layer):
#   docker build --progress=plain -f demo/sim/Dockerfile -t edgefhe-demo-sim:<tag> .
# Run (PowerShell; the evidence folder must be new or empty):
#   docker run --rm --init -p 127.0.0.1:8080:8080 -v C:/EdgeFHE_evidence/<new folder>:/evidence edgefhe-demo-sim:<tag> `
#     bash /repo/demo/sim/rehearse_form.sh [--auto]
# Layout: /repo (the filtered context), /src and /src.OVERLAY_RECORD.json (staged tree and its record), /out (app/, net/,
#   bench/ and the self-test logs), /build -> /out/net (the path that demo/cloud/run_server.sh uses), /opt/efhe
#   (libefhe_keylog.so, run_server.sh, make_demo_pki.sh, OVERLAY_RECORD.json, OPENFHE_SOURCE.txt, CONTEXT_DROPPED.txt,
#   BUILD_SELFTEST.txt and BUILD_SHA256, the build record that run_server.sh and rehearse_form.sh check).

# ----------------------------------------------------------------------------------------------------------- toolchain
FROM ubuntu:22.04 AS toolchain
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential gcc-11 g++-11 cmake git python3 libssl-dev openssl \
      tcpdump ca-certificates iproute2 \
 && rm -rf /var/lib/apt/lists/*
ENV LANG=C.UTF-8

# OpenFHE 1.5.1 exactly as DEPENDENCIES_zh-TW.md:16-27: tag and commit both checked; tests, examples and benchmarks off;
# OpenMP on; NATIVE_SIZE 64; prefix /root/.local/openfhe-v1.5.1. Only the number of parallel compile jobs differs from
# the document (--parallel 3): OPENFHE_JOBS=auto uses min(CPUs, GiB of memory / 3), which changes no installed byte.
# The source and build trees are removed after the install; /opt/efhe/OPENFHE_SOURCE.txt keeps the commit, the tag, the
# submodule commits, the CMake options and the toolchain.
ARG OPENFHE_JOBS=auto
RUN install -d -m 0755 /opt/efhe \
 && mkdir -p /root/edgefhe-dependencies && cd /root/edgefhe-dependencies \
 && git clone --branch v1.5.1 --recursive https://github.com/openfheorg/openfhe-development.git openfhe-1.5.1 \
 && cd openfhe-1.5.1 \
 && test "$(git rev-parse HEAD)" = 1306d14f8c26bb6150d3e6ad54f28dfe1007689e \
 && test "$(git describe --tags --exact-match HEAD)" = v1.5.1 \
 && njobs=$OPENFHE_JOBS \
 && if [ "$njobs" = auto ]; then \
      cpus=$(nproc); mem=$(( $(sed -n 's/^MemTotal: *\([0-9]*\) kB$/\1/p' /proc/meminfo) / 3145728 )); \
      njobs=$(( cpus < mem ? cpus : mem )); if [ "$njobs" -lt 1 ]; then njobs=1; fi; \
    fi \
 && [[ $njobs =~ ^[1-9][0-9]{0,2}$ ]] \
 && options='-DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc-11 -DCMAKE_CXX_COMPILER=g++-11 -DCMAKE_INSTALL_PREFIX=/root/.local/openfhe-v1.5.1 -DBUILD_UNITTESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARKS=OFF -DWITH_OPENMP=ON -DNATIVE_SIZE=64' \
 && cmake -S . -B build $options \
 && cmake --build build --parallel "$njobs" \
 && cmake --install build \
 && test -f /root/.local/openfhe-v1.5.1/lib/OpenFHE/OpenFHEConfig.cmake \
 && { echo '# OpenFHE source of /root/.local/openfhe-v1.5.1 (v8b_rev5/docs/DEPENDENCIES_zh-TW.md:16-27)'; \
      echo 'repository=https://github.com/openfheorg/openfhe-development.git'; \
      echo "tag=$(git describe --tags --exact-match HEAD)"; \
      echo "commit=$(git rev-parse HEAD)"; \
      git submodule status --recursive | sed 's/^/submodule /'; \
      echo "cmake_options=$options"; \
      echo "parallel_jobs=$njobs"; \
      echo "toolchain: $(gcc-11 --version | sed -n 1p); $(g++-11 --version | sed -n 1p); $(cmake --version | sed -n 1p)"; \
    } >/opt/efhe/OPENFHE_SOURCE.txt \
 && cd / && rm -rf /root/edgefhe-dependencies \
 && cat /opt/efhe/OPENFHE_SOURCE.txt \
 && printf 'F_LOCAL_DEPENDENCY_INSTALL_COMPLETE_RUN_RELEASE_SELFTEST_NEXT\n'

# ------------------------------------------------------------------------------------------------------------- sources
FROM ubuntu:22.04 AS sources
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
COPY v8b_rev5 /ctx/v8b_rev5
COPY demo /ctx/demo
# v8b_rev5/ must arrive byte for byte (make_overlay.py runs its verify_repo.py), so only Python caches are dropped there.
# From demo/, local and run directories, build output, captures, logs, keys, certificates, CSRs, pins and archives are
# dropped as well: the rules of demo/cloud/Dockerfile.dockerignore. /opt/efhe/CONTEXT_DROPPED.txt lists what was dropped.
RUN { find /ctx -depth \( -name __pycache__ -o -name '*.pyc' \) -print; \
      find /ctx/demo -depth \( \( -type d \( -name local -o -name runs -o -name build -o -name node_modules \
             -o -name '.venv*' \) \) -o -name .env -o -name '*.pcap' -o -name '*.pcapng' -o -name '*.log' \
             -o -name '*.key' -o -name '*.pem' -o -name '*.csr' -o -name '*.srl' -o -name '*.pin' -o -name '*.p12' \
             -o -name '*.tar' \) -print; \
    } | LC_ALL=C sort -u >/ctx/DROPPED.txt \
 && while IFS= read -r path; do rm -rf -- "$path"; done </ctx/DROPPED.txt \
 && echo "EDGEFHE_SIM_CONTEXT_FILTERED dropped=$(wc -l </ctx/DROPPED.txt)"

# ----------------------------------------------------------------------------------------------------------------- sim
FROM toolchain AS sim
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ARG EDGEFHE_COMMIT=dev
COPY --from=sources /ctx/v8b_rev5 /repo/v8b_rev5
COPY --from=sources /ctx/demo /repo/demo
COPY --from=sources /ctx/DROPPED.txt /opt/efhe/CONTEXT_DROPPED.txt

# Staged overlay outside the repository copy: make_overlay.py verifies the base tree, the staged tree (282 files) and
# every hash in demo/model/EXPECTED.json, and writes /src.OVERLAY_RECORD.json with the staged_tree_id.
RUN python3 -B /repo/demo/overlay/make_overlay.py --repo /repo --out /src --commit "$EDGEFHE_COMMIT"

# The full V8b build of the staged tree and its self-tests (span, permit, wire, application equivalence); the script
# ends with EDGEFHE_BUILD_AND_SELFTEST_PASS or fails this step.
RUN bash /src/tools/build_and_selftest.sh /out /root/.local/openfhe-v1.5.1/lib/OpenFHE 2>&1 \
      | tee /out.build_and_selftest.log \
 && [ "$(tail -n 1 /out.build_and_selftest.log)" = EDGEFHE_BUILD_AND_SELFTEST_PASS ] \
 && mv /out.build_and_selftest.log /out/build_and_selftest.log \
 && cat /out/span.log /out/permit.log /out/wire.log /out/app_equivalence.log >/opt/efhe/BUILD_SELFTEST.txt \
 && echo EDGEFHE_BUILD_AND_SELFTEST_PASS | tee -a /opt/efhe/BUILD_SELFTEST.txt

# /build is the path of the cloud image that demo/cloud/run_server.sh uses; the build-tree RPATH of efnet_server
# (/out/net) still finds libefnet_cloud.so through it. efnet_server must load the system OpenSSL 3 dynamically and import
# SSL_new from it (otherwise LD_PRELOAD could not interpose the key-log hook); efnet_client, the owner library and the
# randomness plugin must resolve every library (the owner library through its RPATH to OpenFHE).
RUN ln -s /out/net /build \
 && ldd /build/efnet_server >/out/efnet_server.ldd \
 && grep -qE '^[[:space:]]*libssl\.so\.3 => /' /out/efnet_server.ldd \
 && nm -D --undefined-only /build/efnet_server >/out/efnet_server.imports \
 && grep -qw SSL_new /out/efnet_server.imports \
 && for f in /out/net/efnet_client /out/app/libef_app_owner.so /out/app/libef_app_normalize.so \
      /out/app/frozen_build/baseline/libedgefhe_os_prng.so; do \
      ldd "$f" >/tmp/ldd.txt; \
      if grep -q 'not found' /tmp/ldd.txt; then cat /tmp/ldd.txt; echo "unresolved library in $f"; exit 1; fi; \
    done \
 && rm -f /tmp/ldd.txt \
 && echo 'EDGEFHE_SIM_LINKS_PASS efnet_server imports SSL_new from libssl.so.3; efnet_client and the owner libraries resolve' \
    | tee -a /opt/efhe/BUILD_SELFTEST.txt

# The demo-only key-log shim with the build-time checks of demo/cloud/Dockerfile: inert without EFHE_DEMO_KEYLOG; with
# it, a new 0600 file and the warning line; an existing file, a symbolic link or a relative path stops the process with
# status 3 before main.
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
 && rm -rf "$t" /tmp/shim.symbols \
 && echo 'EDGEFHE_KEYLOG_SHIM_BUILD_CHECKS_PASS inert create_0600 refuse_existing refuse_symlink refuse_relative' \
    | tee -a /opt/efhe/BUILD_SELFTEST.txt

# The cloud launcher and the demo PKI script (LF only), the rehearsal scripts (LF only, syntax and offline self-test), the
# overlay record (its staged_tree_id must equal demo/model/EXPECTED.json) and the build record: SHA-256 of the binaries,
# the libraries in use, the shim, the scripts, the records and every file of /repo/demo (sha256sum -c format).
RUN for f in /repo/demo/cloud/run_server.sh /repo/demo/pki/make_demo_pki.sh /repo/demo/sim/rehearse_form.sh \
      /repo/demo/sim/drive_presets.py /repo/demo/sim/gateway.sim.json; do \
      if grep -q $'\r' "$f"; then echo "CR bytes in $f: these files must be LF-only"; exit 1; fi; \
    done \
 && install -m 0755 /repo/demo/cloud/run_server.sh /repo/demo/pki/make_demo_pki.sh /opt/efhe/ \
 && sh -n /opt/efhe/run_server.sh && bash -n /opt/efhe/make_demo_pki.sh && bash -n /repo/demo/sim/rehearse_form.sh \
 && python3 -I -B /repo/demo/sim/drive_presets.py --selftest | tee -a /opt/efhe/BUILD_SELFTEST.txt \
 && install -m 0644 /src.OVERLAY_RECORD.json /opt/efhe/OVERLAY_RECORD.json \
 && tree_id=$(python3 -I -B -c 'import json; print(json.load(open("/opt/efhe/OVERLAY_RECORD.json"))["staged_tree_id"])') \
 && expected=$(python3 -I -B -c 'import json; print(json.load(open("/repo/demo/model/EXPECTED.json"))["staged_tree_id"])') \
 && [[ $tree_id =~ ^[0-9a-f]{64}$ ]] && [ "$tree_id" = "$expected" ] \
 && libssl=$(readlink -f "$(sed -n 's/^[[:space:]]*libssl\.so\.3 => \(\/[^ ]*\) .*/\1/p' /out/efnet_server.ldd)") \
 && libcrypto=$(readlink -f "$(sed -n 's/^[[:space:]]*libcrypto\.so\.3 => \(\/[^ ]*\) .*/\1/p' /out/efnet_server.ldd)") \
 && mapfile -t openfhe < <(find /root/.local/openfhe-v1.5.1/lib -maxdepth 1 -type f -name 'libOPENFHE*.so*' | LC_ALL=C sort) \
 && [ "${#openfhe[@]}" -ge 3 ] \
 && mapfile -t demo < <(find /repo/demo -type f | LC_ALL=C sort) \
 && { echo '# EdgeFHE demo PC rehearsal image: build record (sha256sum -c format; lines starting with # are comments)'; \
      echo "# edgefhe_commit=$EDGEFHE_COMMIT"; \
      echo "# staged_tree_id=$tree_id"; \
      echo "# openfhe=$(sed -n 's/^commit=//p' /opt/efhe/OPENFHE_SOURCE.txt) $(sed -n 's/^tag=//p' /opt/efhe/OPENFHE_SOURCE.txt)"; \
      echo "# toolchain: $(g++-11 --version | sed -n 1p); $(cmake --version | sed -n 1p); $(openssl version); $(ldd --version | sed -n 1p); $(python3 --version)"; \
      sha256sum /build/efnet_server /build/libefnet_cloud.so /build/efnet_probe /build/efnet_span_selftest \
        /build/efnet_permit_selftest /build/efnet_wire_selftest /build/efnet_wire_fixture /out/net/efnet_client \
        /out/app/libef_app_owner.so /out/app/libef_app_normalize.so /out/app/frozen_build/baseline/libedgefhe_os_prng.so \
        "${openfhe[@]}" "$libssl" "$libcrypto" /opt/efhe/libefhe_keylog.so /opt/efhe/run_server.sh \
        /opt/efhe/make_demo_pki.sh /opt/efhe/OVERLAY_RECORD.json /opt/efhe/OPENFHE_SOURCE.txt \
        /opt/efhe/CONTEXT_DROPPED.txt /opt/efhe/BUILD_SELFTEST.txt "${demo[@]}"; \
    } >/opt/efhe/BUILD_SHA256 \
 && (cd / && sha256sum --check --strict --quiet /opt/efhe/BUILD_SHA256) \
 && cat /opt/efhe/BUILD_SHA256

LABEL org.opencontainers.image.title="edgefhe-demo-sim" \
      org.opencontainers.image.description="EdgeFHE demo PC rehearsal: gateway, owner screen, unchanged efnet_client (backend spec, PL bit-exact software model) and efnet_server with a demo-only TLS key-log shim; rehearsal only" \
      org.opencontainers.image.revision="$EDGEFHE_COMMIT"
EXPOSE 8080/tcp
CMD ["bash", "/repo/demo/sim/rehearse_form.sh"]
