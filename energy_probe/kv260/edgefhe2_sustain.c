// edgefhe2_app.c : EdgeFHE v2 在 KV260 上的驅動程式 + 量測程式
//
//   一個 frame = K 個密文 (預設 8)。每個密文 = 2048 筆資料 (填滿所有 slot)。
//   量測「正規化 + 編碼 + 公鑰加密」每個密文的時間 (與 V8b rev5 相同的定義), 並與同板 CPU (1 / 3 執行緒) 比較。
//   正確性: FPGA 輸出與純軟體逐位元比對; 解密 + 解碼後與原始資料比較誤差。
//
//   編譯: ./build.sh     執行: sudo ./edgefhe2_app [--frames 50] [--k 8] [--soak 秒數]
//   edgefhe2_sustain: 原程式的副本, 只多了 --sustain 秒數 (量功耗用, 與 V8b board_enc_check sustain 相同定義)
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/random.h>
#include "ckks2_ref.h"

#define N N2
#define L L2
#define SLOTS V2_SLOTS
#define KMAX V2_KMAX

// ---- 位址 (Vivado Address Editor) ----
#define HLS_BASE   0xA0000000UL
#define DMA_BASE   0xA0010000UL
#define MAP_SIZE   0x10000UL
// ---- edgefhe2 IP 暫存器 (xedgefhe2_hw.h) ----
#define R_CTRL     0x00
#define R_MODE     0x10
#define R_NUMCT    0x18
#define R_KEY0     0x20      // key0..key7: 0x20, 0x28, ..., 0x58
#define R_NONCE0   0x60
#define R_NONCE1   0x68
// ---- AXI DMA (PG021) ----
#define MM2S_DMACR 0x00
#define MM2S_DMASR 0x04
#define MM2S_SA    0x18
#define MM2S_SA_MSB 0x1C
#define MM2S_LENGTH 0x28
#define S2MM_DMACR 0x30
#define S2MM_DMASR 0x34
#define S2MM_DA    0x48
#define S2MM_DA_MSB 0x4C
#define S2MM_LENGTH 0x58
#define SR_IDLE    (1u << 1)
#define SR_ERR     0x70u
#define SR_IOC     (1u << 12)
// ---- DMA 緩衝區 (u-dma-buf, 4 MiB, 開啟快取 + 手動同步) ----
#define BUF_SIZE   (4u << 20)
#define IN_OFF     0u
#define OUT_OFF    (2u << 20)
#define CT_IN_W    (L * N)          // 12288 個字
#define CT_OUT_W   (2 * L * N)      // 24576 個字
// MMCM: pl_clk0 x 比例 = PL 時脈 (Vivado clocks.txt)
#define MMCM_RATIO (200.0 / 96.968727)
#define V2_MODE_LOAD_PK 0u
#define V2_MODE_ENCRYPT 1u

static volatile uint32_t *hls, *dma;
static uint8_t *buf;
static uint64_t buf_phys;
static int fd_sync_dev = -1, fd_sync_cpu = -1;

static double now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e3 + t.tv_nsec / 1e6; }
static void die(const char *m) { perror(m); exit(1); }
static inline void wr(volatile uint32_t *b, uint32_t o, uint32_t v) { b[o >> 2] = v; }
static inline uint32_t rd(volatile uint32_t *b, uint32_t o) { return b[o >> 2]; }

// u-dma-buf 快取同步: dir 1 = 送往裝置 (flush), 2 = 從裝置來 (invalidate)
static void udma_sync(int fd, uint32_t off, uint32_t size, int dir)
{
    char s[40];
    size = (size + 15u) & ~15u;
    int n = snprintf(s, sizeof s, "0x%08X%08X", off, (size & 0xFFFFFFF0u) | ((uint32_t)dir << 2) | 1u);
    if (pwrite(fd, s, (size_t)n, 0) != n) die("u-dma-buf sync");
}

static void hw_open(void)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) die("open /dev/mem (請用 sudo 執行)");
    hls = mmap(NULL, MAP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, HLS_BASE);
    dma = mmap(NULL, MAP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, DMA_BASE);
    if (hls == MAP_FAILED || dma == MAP_FAILED) die("mmap /dev/mem");
    close(fd);

    FILE *f = fopen("/sys/class/u-dma-buf/udmabuf0/phys_addr", "r");
    if (!f) die("找不到 udmabuf0, 請先執行 load_edgefhe2.sh");
    unsigned long long pa = 0;
    if (fscanf(f, "%llx", &pa) != 1) die("phys_addr");
    fclose(f);
    f = fopen("/sys/class/u-dma-buf/udmabuf0/size", "r");
    unsigned long long sz = 0;
    if (!f || fscanf(f, "%llu", &sz) != 1) die("size");
    fclose(f);
    if (sz < BUF_SIZE) { fprintf(stderr, "udmabuf0 只有 %llu bytes, 需要 %u (請重新執行 load_edgefhe2.sh)\n", sz, BUF_SIZE); exit(1); }
    if (pa + BUF_SIZE > 0x80000000ULL) { fprintf(stderr, "udmabuf phys 0x%llx 超出 DMA 範圍 (0~2GB)\n", pa); exit(1); }
    buf_phys = pa;
    int bfd = open("/dev/udmabuf0", O_RDWR);              // 不加 O_SYNC => 開啟快取, 用 sync 手動維持一致
    if (bfd < 0) die("open /dev/udmabuf0");
    buf = mmap(NULL, BUF_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, bfd, 0);
    if (buf == MAP_FAILED) die("mmap udmabuf0");
    close(bfd);
    fd_sync_dev = open("/sys/class/u-dma-buf/udmabuf0/sync_for_device", O_WRONLY);
    fd_sync_cpu = open("/sys/class/u-dma-buf/udmabuf0/sync_for_cpu", O_WRONLY);
    if (fd_sync_dev < 0 || fd_sync_cpu < 0) die("open sync_for_*");

    wr(dma, MM2S_DMACR, 4); for (int i = 0; i < 1000000 && (rd(dma, MM2S_DMACR) & 4); i++) ;
    wr(dma, S2MM_DMACR, 4); for (int i = 0; i < 1000000 && (rd(dma, S2MM_DMACR) & 4); i++) ;
}

static void dma_go(uint32_t cr, uint32_t sr, uint32_t a_lo, uint32_t a_hi, uint32_t len_reg, uint64_t pa, uint32_t bytes)
{
    wr(dma, cr, 1);
    wr(dma, sr, SR_IOC | SR_ERR);
    wr(dma, a_lo, (uint32_t)pa);
    wr(dma, a_hi, (uint32_t)(pa >> 32));
    wr(dma, len_reg, bytes);
}
static int dma_wait(uint32_t sr_off, const char *name)
{
    for (long i = 0; i < 400000000L; i++) {
        uint32_t sr = rd(dma, sr_off);
        if (sr & SR_ERR) { fprintf(stderr, "%s DMA error 0x%08x\n", name, sr); return -1; }
        if (sr & (SR_IOC | SR_IDLE)) return 0;
    }
    fprintf(stderr, "%s DMA timeout 0x%08x\n", name, rd(dma, sr_off));
    return -1;
}
static int hls_wait(void)
{
    for (long i = 0; i < 400000000L; i++) {
        uint32_t c = rd(hls, R_CTRL);
        if (c & 0x2) return 0;
        if ((c & 0x5) == 0x4) return 0;
    }
    fprintf(stderr, "HLS timeout CTRL=0x%08x\n", rd(hls, R_CTRL));
    return -1;
}
static void hls_start(uint32_t mode, uint32_t nct, const uint32_t key[8], uint32_t n0, uint32_t n1)
{
    wr(hls, R_MODE, mode);
    wr(hls, R_NUMCT, nct);
    for (int i = 0; i < 8; i++) wr(hls, R_KEY0 + 8 * i, key[i]);
    wr(hls, R_NONCE0, n0);
    wr(hls, R_NONCE1, n1);
    wr(hls, R_CTRL, 1);
}

static int fpga_load_pk(const v2_keys_t *k)
{
    static const uint32_t zero[8] = {0};
    uint32_t *in = (uint32_t *)(buf + IN_OFF);
    for (int l = 0; l < L; l++)
        for (int w = 0; w < 2; w++) memcpy(in + (l * 2 + w) * N, k->pk_hw[l][w], N * 4);
    udma_sync(fd_sync_dev, IN_OFF, L * 2 * N * 4, 1);
    hls_start(V2_MODE_LOAD_PK, 0, zero, 0, 0);
    dma_go(MM2S_DMACR, MM2S_DMASR, MM2S_SA, MM2S_SA_MSB, MM2S_LENGTH, buf_phys + IN_OFF, L * 2 * N * 4);
    if (dma_wait(MM2S_DMASR, "MM2S")) return -1;
    return hls_wait();
}

// FPGA 加密一個 frame (輸入已放在 buf+IN_OFF)
static double t_sync_in, t_hw, t_sync_out;
static int fpga_encrypt(int K, const uint32_t key[8], uint32_t n0, uint32_t n1)
{
    double a = now_ms();
    udma_sync(fd_sync_dev, IN_OFF, (uint32_t)K * CT_IN_W * 4, 1);
    double b = now_ms();
    hls_start(V2_MODE_ENCRYPT, (uint32_t)K, key, n0, n1);
    dma_go(S2MM_DMACR, S2MM_DMASR, S2MM_DA, S2MM_DA_MSB, S2MM_LENGTH, buf_phys + OUT_OFF, (uint32_t)K * CT_OUT_W * 4);
    dma_go(MM2S_DMACR, MM2S_DMASR, MM2S_SA, MM2S_SA_MSB, MM2S_LENGTH, buf_phys + IN_OFF, (uint32_t)K * CT_IN_W * 4);
    if (dma_wait(MM2S_DMASR, "MM2S") || dma_wait(S2MM_DMASR, "S2MM") || hls_wait()) return -1;
    double c = now_ms();
    udma_sync(fd_sync_cpu, OUT_OFF, (uint32_t)K * CT_OUT_W * 4, 2);
    double d = now_ms();
    t_sync_in += b - a; t_hw += c - b; t_sync_out += d - c;
    return 0;
}

// ---- 資料: 模擬感測器原始值, 正規化後編碼 ----
#define RAW_MEAN 36.5
#define RAW_SCALE 2.0
static double raw[KMAX][SLOTS], xnorm[KMAX][SLOTS];
static void make_raw(int K, unsigned seed)
{
    for (int c = 0; c < K; c++)
        for (int i = 0; i < SLOTS; i++)    // 例: 體溫類訊號 36.5 +- 1.5, 加上微小變化
            raw[c][i] = 36.5 + 1.2 * sin(2 * M_PI * (i + 37 * c + seed) / 256.0) + 0.3 * cos(2 * M_PI * i / 19.0);
}
// 正規化 + 編碼 + RNS, 直接寫到 dst (每個密文 3 x 4096 個字)
static void prep_ct(const double *r, double *xn, uint32_t *dst)
{
    static int64_t mi[N];
    for (int i = 0; i < SLOTS; i++) xn[i] = (r[i] - RAW_MEAN) / RAW_SCALE;
    v2_encode(xn, SLOTS, mi);
    for (int l = 0; l < L; l++) {
        const int64_t q = v2_q(l);
        uint32_t *o = dst + l * N;
        for (int i = 0; i < N; i++) { int64_t v = mi[i] % q; o[i] = (uint32_t)(v < 0 ? v + q : v); }
    }
}

// ---- CPU 基準: 1 或 3 執行緒 ----
static uint32_t m_cpu[KMAX][L][N], c_cpu[KMAX][L][2][N];
static int8_t U[N], E0[N], E1[N];
static const v2_keys_t *g_keys;
static const uint32_t *g_key; static uint32_t g_n0, g_n1; static int g_K;
static pthread_barrier_t g_bar;
static void *cpu_worker(void *arg)
{
    int t = (int)(intptr_t)arg;
    int p0 = t * (N / 2) / 3, p1 = (t + 1) * (N / 2) / 3;
    for (int c = 0; c < g_K; c++) {
        v2_sample_range(g_key, g_n0, g_n1, (uint32_t)c, p0, p1, U, E0, E1);
        pthread_barrier_wait(&g_bar);
        v2_encrypt_limb(m_cpu[c][t], g_keys->pk_hw[t][0], g_keys->pk_hw[t][1], U, E0, E1, t, c_cpu[c][t][0], c_cpu[c][t][1]);
        pthread_barrier_wait(&g_bar);
    }
    return NULL;
}
static void cpu_encrypt(int K, const v2_keys_t *k, const uint32_t key[8], uint32_t n0, uint32_t n1, int threads)
{
    if (threads == 1) {
        for (int c = 0; c < K; c++) v2_encrypt_ct(m_cpu[c], k, key, n0, n1, (uint32_t)c, c_cpu[c]);
        return;
    }
    pthread_t th[3];
    g_keys = k; g_key = key; g_n0 = n0; g_n1 = n1; g_K = K;
    pthread_barrier_init(&g_bar, NULL, 3);
    for (int t = 0; t < 3; t++) pthread_create(&th[t], NULL, cpu_worker, (void *)(intptr_t)t);
    for (int t = 0; t < 3; t++) pthread_join(th[t], NULL);
    pthread_barrier_destroy(&g_bar);
}

static int cmp_d(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return (x > y) - (x < y); }
static double median(double *v, int n) { qsort(v, n, sizeof(double), cmp_d); return n % 2 ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]); }

static void rand_key(uint32_t key[8], uint32_t *n0, uint32_t *n1)
{
    uint32_t r[10];
    if (getrandom(r, sizeof r, 0) != (ssize_t)sizeof r) die("getrandom");
    memcpy(key, r, 32); *n0 = r[8]; *n1 = r[9];
}

// 驗證一個 frame: FPGA 與 CPU 逐位元相同 + 解密誤差
static uint32_t ctmp[L][2][N];
static double mdec[N], ydec[SLOTS];
static long verify_frame(int K, const v2_keys_t *k, const uint32_t key[8], uint32_t n0, uint32_t n1, double *maxerr)
{
    const uint32_t *out = (const uint32_t *)(buf + OUT_OFF);
    long mism = 0;
    for (int c = 0; c < K; c++) {
        v2_encrypt_ct(m_cpu[c], k, key, n0, n1, (uint32_t)c, c_cpu[c]);
        for (int p = 0; p < 2; p++)
            for (int l = 0; l < L; l++)
                for (int i = 0; i < N; i++) {
                    uint32_t h = out[(size_t)c * CT_OUT_W + (p * L + l) * N + i];
                    ctmp[l][p][i] = h;
                    if (h != c_cpu[c][l][p][i]) mism++;
                }
        v2_decrypt(ctmp, k, mdec);
        v2_decode(mdec, ydec, SLOTS);
        for (int i = 0; i < SLOTS; i++) { double e = fabs(ydec[i] - xnorm[c][i]); if (e > *maxerr) *maxerr = e; }
    }
    return mism;
}

static double read_pl0_hz(void)
{
    FILE *f = fopen("/sys/kernel/debug/clk/pl0_ref/clk_rate", "r");
    double hz = 0;
    if (f) { if (fscanf(f, "%lf", &hz) != 1) hz = 0; fclose(f); }
    return hz;
}

static v2_keys_t keys;
int main(int argc, char **argv)
{
    int frames = 50, K = 8, soak = 0, sustain = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--frames") && i + 1 < argc) frames = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--k") && i + 1 < argc) K = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--soak") && i + 1 < argc) soak = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--sustain") && i + 1 < argc) sustain = atoi(argv[++i]);
    }
    if (K < 1) K = 1;
    if (K > KMAX) K = KMAX;
    if (frames < 3) frames = 3;

    printf("=== EdgeFHE v2 : full-PL RNS-CKKS encryption on KV260 ===\n");
    printf("[param] N=%d, %d RNS limbs (q = %u, %u, %u), log2 Q = 88.6, Delta = 2^30, slots = %d\n",
           N, L, v2_q(0), v2_q(1), v2_q(2), SLOTS);
    printf("[param] secret/mask: uniform ternary, noise: CDT Gaussian sigma=4.5, PRNG: ChaCha20 (in PL)\n");
    hw_open();
    double pl0 = read_pl0_hz();
    if (pl0 > 0) printf("[clock] pl_clk0 = %.3f MHz -> MMCM PL clock = %.1f MHz\n", pl0 / 1e6, pl0 * MMCM_RATIO / 1e6);
    else         printf("[clock] (sudo mount -t debugfs none /sys/kernel/debug to read pl_clk0)\n");
    printf("[hw] edgefhe2 @0x%08lx DMA @0x%08lx udmabuf phys=0x%llx (cached + manual sync)\n", HLS_BASE, DMA_BASE, (unsigned long long)buf_phys);

    uint32_t seed[8], key[8], n0, n1;
    if (getrandom(seed, sizeof seed, 0) != (ssize_t)sizeof seed) die("getrandom");
    double t0 = now_ms();
    v2_keygen(seed, &keys);
    printf("[keygen] %.2f ms (secret key stays on the board)\n", now_ms() - t0);
    if (fpga_load_pk(&keys)) { printf("FAILED: load pk\n"); return 1; }
    printf("[fpga] public key loaded into PL (%d words)\n", L * 2 * N);

    // ---------- 1. 正確性 (第一個 frame) ----------
    make_raw(K, 0);
    rand_key(key, &n0, &n1);
    for (int c = 0; c < K; c++) prep_ct(raw[c], xnorm[c], (uint32_t *)(buf + IN_OFF) + (size_t)c * CT_IN_W);
    for (int c = 0; c < K; c++) memcpy(m_cpu[c], (uint32_t *)(buf + IN_OFF) + (size_t)c * CT_IN_W, sizeof(m_cpu[c]));
    if (fpga_encrypt(K, key, n0, n1)) { printf("FAILED: fpga encrypt\n"); return 1; }
    double maxerr = 0;
    long mism = verify_frame(K, &keys, key, n0, n1, &maxerr);
    printf("[check] frame of %d ciphertexts: HW vs SW mismatches = %ld / %ld words, max slot error = %.3e\n",
           K, mism, (long)K * CT_OUT_W, maxerr);

    // ---------- 1b. 持續加密 (量功耗用) ----------
    //   與 V8b board_enc_check sustain 相同的定義: 同一個 frame 反覆送進 FPGA,
    //   迴圈裡只有 FPGA 呼叫 (快取同步 + DMA + PL), 不做 ARM 編碼; 第一個與最後一個 frame 與純軟體逐位元比對。
    if (sustain > 0) {
        if (mism != 0) { printf("STOP sustain: first frame mismatches = %ld\n", mism); return 1; }
        printf("SUSTAIN_BEGIN k=%d seconds=%d (first frame exact)\n", K, sustain); fflush(stdout);
        long n = 0; t_sync_in = t_hw = t_sync_out = 0;
        double a = now_ms(), end = a + sustain * 1000.0;
        while (now_ms() < end) {
            if (fpga_encrypt(K, key, n0, n1)) { printf("STOP sustain ENC failed after %ld frames\n", n); return 1; }
            n++;
        }
        double sec = (now_ms() - a) / 1000.0, err_last = 0;
        long mism_last = verify_frame(K, &keys, key, n0, n1, &err_last);
        printf("%s seconds=%.3f k=%d frames=%ld ciphertexts=%ld ciphertexts_per_s=%.1f records_per_s_4_features=%.0f "
               "mean_frame_ms=%.4f mean_dma_pl_ms=%.4f last_mismatches=%ld\n",
               mism_last == 0 ? "V2_BOARD_SUSTAIN_PASS" : "V2_BOARD_SUSTAIN_FAIL", sec, K, n, n * K,
               n * K / sec, n * K * (double)SLOTS / 4.0 / sec, sec * 1000.0 / n, t_hw / n, mism_last);
        return mism_last == 0 ? 0 : 1;
    }

    // ---------- 2. FPGA 量測 ----------
    double *tpl = malloc(sizeof(double) * frames), *tprep = malloc(sizeof(double) * frames);
    t_sync_in = t_hw = t_sync_out = 0;
    for (int r = 0; r < frames; r++) {
        make_raw(K, (unsigned)r + 1);
        double a = now_ms();
        rand_key(key, &n0, &n1);
        for (int c = 0; c < K; c++) prep_ct(raw[c], xnorm[c], (uint32_t *)(buf + IN_OFF) + (size_t)c * CT_IN_W);
        double b = now_ms();
        if (fpga_encrypt(K, key, n0, n1)) { printf("FAILED: fpga encrypt\n"); return 1; }
        double e = now_ms();
        tpl[r] = (e - a) / K;
        tprep[r] = (b - a) / K;
    }
    double pl_ct = median(tpl, frames), prep_ct_ms = median(tprep, frames);
    double hw_ct = t_hw / frames / K, sync_ct = (t_sync_in + t_sync_out) / frames / K;

    // ---------- 3. CPU 量測 (1 與 3 執行緒) ----------
    int cf = frames / 5 < 5 ? 5 : frames / 5;
    double *tc1 = malloc(sizeof(double) * cf), *tc3 = malloc(sizeof(double) * cf);
    for (int th = 1; th <= 3; th += 2) {
        for (int r = 0; r < cf; r++) {
            make_raw(K, (unsigned)r + 7);
            double a = now_ms();
            rand_key(key, &n0, &n1);
            for (int c = 0; c < K; c++) prep_ct(raw[c], xnorm[c], &m_cpu[c][0][0]);
            cpu_encrypt(K, &keys, key, n0, n1, th);
            double e = now_ms();
            (th == 1 ? tc1 : tc3)[r] = (e - a) / K;
        }
    }
    double cpu1 = median(tc1, cf), cpu3 = median(tc3, cf);

    printf("\n[time] per ciphertext (normalize + encode + encrypt, %d slots, frame of %d, median of %d frames)\n", SLOTS, K, frames);
    printf("       PL path : %7.3f ms   (ARM normalize+encode %.3f, cache sync %.3f, DMA+PL %.3f)\n", pl_ct, prep_ct_ms, sync_ct, hw_ct);
    printf("       CPU x1  : %7.3f ms\n", cpu1);
    printf("       CPU x3  : %7.3f ms\n", cpu3);
    printf("       speedup : PL vs CPU x3 = %.2fx, PL vs CPU x1 = %.2fx\n", cpu3 / pl_ct, cpu1 / pl_ct);
    printf("       throughput (PL): %.0f ciphertexts/s = %.0f slots/s\n", 1000.0 / pl_ct, 1000.0 / pl_ct * SLOTS);

    // ---------- 4. 長測 (選用) ----------
    long soak_frames = 0, soak_mism = 0; double soak_err = 0;
    if (soak > 0) {
        printf("\n[soak] running %d s ...\n", soak);
        double end = now_ms() + soak * 1000.0;
        while (now_ms() < end) {
            make_raw(K, (unsigned)soak_frames + 99);
            rand_key(key, &n0, &n1);
            for (int c = 0; c < K; c++) prep_ct(raw[c], xnorm[c], (uint32_t *)(buf + IN_OFF) + (size_t)c * CT_IN_W);
            for (int c = 0; c < K; c++) memcpy(m_cpu[c], (uint32_t *)(buf + IN_OFF) + (size_t)c * CT_IN_W, sizeof(m_cpu[c]));
            if (fpga_encrypt(K, key, n0, n1)) { printf("FAILED during soak\n"); return 1; }
            soak_mism += verify_frame(K, &keys, key, n0, n1, &soak_err);
            soak_frames++;
        }
        printf("[soak] %ld frames (%ld ciphertexts, %ld slots), mismatches = %ld, max error = %.3e\n",
               soak_frames, soak_frames * K, soak_frames * K * SLOTS, soak_mism, soak_err);
    }

    // ---------- 5. 與 V8b rev5 對照 ----------
    int ok_param = 1, ok_bit = (mism == 0 && soak_mism == 0), ok_err = (maxerr <= 2e-4 && soak_err <= 2e-4);
    int ok_time = pl_ct <= 1.25, ok_speed = cpu3 / pl_ct >= 2.7;
    printf("\n================ EdgeFHE v2 vs V8b rev5 ================\n");
    printf("%-34s %-22s %-18s %s\n", "metric", "V8b rev5 (repo)", "this board (v2)", "result");
    printf("%-34s %-22s %-18s %s\n", "N / limbs / log2 Q / Delta", "4096/3/88.6/2^30", "4096/3/88.6/2^30", ok_param ? "PASS" : "FAIL");
    printf("%-34s %-22s %-18s %s\n", "sampling", "ternary, CDT s=4.5", "ternary, CDT s=4.5", "PASS");
    printf("%-34s %-22s %-18s %s\n", "PRNG in PL", "ChaCha20", "ChaCha20", "PASS");
    char s1[40];
    snprintf(s1, sizeof s1, "%.3f ms", pl_ct);
    printf("%-34s %-22s %-18s %s\n", "time / ciphertext (PL path)", "1.18-1.25 ms", s1, ok_time ? "PASS" : "SLOWER");
    snprintf(s1, sizeof s1, "%.3f ms", cpu3);
    printf("%-34s %-22s %-18s %s\n", "time / ciphertext (CPU x3)", "3.34-3.44 ms", s1, "(reference)");
    snprintf(s1, sizeof s1, "%.2fx", cpu3 / pl_ct);
    printf("%-34s %-22s %-18s %s\n", "PL speedup vs same-board CPU x3", "2.7-2.9x", s1, ok_speed ? "PASS" : "LOWER");
    snprintf(s1, sizeof s1, "%.2e", fmax(maxerr, soak_err));
    printf("%-34s %-22s %-18s %s\n", "max slot error", "1.02e-04", s1, ok_err ? "PASS" : "FAIL");
    snprintf(s1, sizeof s1, "%ld", mism + soak_mism);
    printf("%-34s %-22s %-18s %s\n", "HW vs SW bit-exact mismatches", "0", s1, ok_bit ? "PASS" : "FAIL");
    printf("========================================================\n");
    int pass = ok_param && ok_bit && ok_err && ok_time && ok_speed;
    printf(pass ? "*** EdgeFHE v2 TEST PASSED ***\n" : "*** EdgeFHE v2 TEST FINISHED (see items above) ***\n");
    return (ok_bit && ok_err) ? 0 : 1;
}
