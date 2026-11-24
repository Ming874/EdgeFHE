/*
 * DEMO ONLY: TLS key-log shim for the unchanged efnet_server in the edgefhe-demo-cloud image.
 *
 * Purpose: a third party decrypts the demo capture with Wireshark and checks that only EFHEW001 ciphertext frames
 * crossed the network. efnet_server, efnet_client and tls.hpp stay unchanged; this library is loaded with LD_PRELOAD
 * into the cloud server only, and only when run_server.sh is started with --keylog. Anyone who holds the key log can
 * read the captured TLS traffic, so it must never be loaded into a production service.
 *
 * Semantics:
 *   - Inert unless EFHE_DEMO_KEYLOG is set and non-empty: SSL_new is then forwarded unchanged.
 *   - When it is set, the constructor (before main) creates that file with
 *     O_WRONLY|O_CREAT|O_EXCL|O_APPEND|O_NOFOLLOW|O_CLOEXEC and mode 0600, prints WARNING_DEMO_TLS_KEYLOG_ENABLED on
 *     stderr and removes EFHE_DEMO_KEYLOG from the environment (a child process finds the shim inert). The path must be
 *     absolute. If the file cannot be created (it exists, it is a symbolic link, the directory is missing, ...), the
 *     process stops with exit status 3 before main runs.
 *   - SSL_new hook (efnet_server creates one SSL per connection, tls.hpp:98): it installs SSL_CTX_set_keylog_callback on
 *     the connection's context and forwards to the real SSL_new found with dlsym(RTLD_NEXT). OpenSSL then hands over
 *     one NSS key-log line per secret, five per TLS 1.3 connection (CLIENT/SERVER_HANDSHAKE_TRAFFIC_SECRET,
 *     CLIENT/SERVER_TRAFFIC_SECRET_0, EXPORTER_SECRET); each line plus LF is appended with a single writev.
 *   - If a line cannot be written completely, the process stops with exit status 4, so that an evidence run never
 *     continues with an incomplete key log.
 *
 * Build (demo/cloud/Dockerfile):
 *   gcc -O2 -Wall -Wextra -Werror -shared -fPIC -o /opt/efhe/libefhe_keylog.so efhe_keylog_preload.c -ldl -lssl -lcrypto
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <openssl/ssl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <unistd.h>

typedef SSL *(*ssl_new_fn)(SSL_CTX *);

static ssl_new_fn real_ssl_new = NULL;
static int keylog_fd = -1;
static char keylog_path[4096];

static void stop(int status, const char *code, int err, const char *reason)
{
    fprintf(stderr, "%s errno=%d (%s) path=%s %s\n", code, err, err ? strerror(err) : "-",
            keylog_path[0] ? keylog_path : "-", reason);
    _exit(status);
}

static void keylog_line(const SSL *ssl, const char *line)
{
    struct iovec iov[2];
    size_t length;
    ssize_t written;

    (void)ssl;
    if (keylog_fd < 0 || line == NULL)
        return;
    length = strlen(line);
    iov[0].iov_base = (void *)line;
    iov[0].iov_len = length;
    iov[1].iov_base = (void *)"\n";
    iov[1].iov_len = 1;
    do {
        written = writev(keylog_fd, iov, 2);
    } while (written < 0 && errno == EINTR);
    if (written != (ssize_t)(length + 1))
        stop(4, "STOP_DEMO_TLS_KEYLOG_WRITE", written < 0 ? errno : 0,
             "本伺服器無法完整寫入 TLS 金鑰紀錄，因此立即停止，以免證據不完整。");
}

__attribute__((constructor)) static void keylog_init(void)
{
    const char *path = getenv("EFHE_DEMO_KEYLOG");
    void *symbol = dlsym(RTLD_NEXT, "SSL_new");

    if (symbol == NULL)
        stop(3, "STOP_DEMO_TLS_KEYLOG_RESOLVE", 0, "本程式找不到 OpenSSL 的 SSL_new，金鑰紀錄墊片無法運作。");
    memcpy(&real_ssl_new, &symbol, sizeof real_ssl_new);
    if (path == NULL || path[0] == '\0')
        return;
    if (strlen(path) >= sizeof keylog_path)
        stop(3, "STOP_DEMO_TLS_KEYLOG_OPEN", ENAMETOOLONG, "金鑰紀錄檔的路徑過長；本伺服器不啟動。");
    memcpy(keylog_path, path, strlen(path) + 1);
    if (path[0] != '/')
        stop(3, "STOP_DEMO_TLS_KEYLOG_OPEN", EINVAL, "金鑰紀錄檔必須以絕對路徑指定；本伺服器不啟動。");
    keylog_fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (keylog_fd < 0)
        stop(3, "STOP_DEMO_TLS_KEYLOG_OPEN", errno,
             "本伺服器無法建立金鑰紀錄檔，因此不啟動；該檔案不得預先存在，也不得是符號連結。");
    if (fchmod(keylog_fd, 0600) != 0)
        stop(3, "STOP_DEMO_TLS_KEYLOG_OPEN", errno, "本伺服器無法把金鑰紀錄檔設為僅擁有者可讀寫，因此不啟動。");
    unsetenv("EFHE_DEMO_KEYLOG");
    fprintf(stderr, "WARNING_DEMO_TLS_KEYLOG_ENABLED path=%s %s\n", keylog_path,
            "示範專用：本伺服器將每一條 TLS 連線的工作階段金鑰寫入此檔；持有此檔者能解開本次擷取的 TLS 流量。");
}

SSL *SSL_new(SSL_CTX *ctx)
{
    if (keylog_fd >= 0 && ctx != NULL && SSL_CTX_get_keylog_callback(ctx) != keylog_line)
        SSL_CTX_set_keylog_callback(ctx, keylog_line);
    return real_ssl_new(ctx);
}
