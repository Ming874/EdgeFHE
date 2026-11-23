// EdgeFHE 能耗探針：ESP32-S3（N16R8）+ INA228（R015）+ ILI9341 顯示面板
#include <Wire.h>
#include <SPI.h>
#include <Adafruit_INA228.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>

// ===== 1. 腳位（ESP32-S3 N16R8）=====
const int SDA_PIN = 17, SCL_PIN = 18;                      // INA228：I2C
const int TFT_SCK = 12, TFT_MOSI = 11, TFT_MISO = 13;      // ILI9341：SPI
const int TFT_CS = 10, TFT_DC = 14, TFT_RST = 4;
// 不可使用：GPIO 26–37（快閃記憶體與 PSRAM）、0／3／45／46（開機設定腳）、
//          19／20（原生 USB）、43／44（序列埠）、48（板上 RGB LED）

// ===== 2. 校正值：第 5 章求出後填入 =====
const float K_I = 1.0000;          // 電流增益
const float K_V = 1.0000;          // 電壓增益
const float I_OFFSET_mA = 0.0;     // 零點偏移（mA）

Adafruit_INA228 ina228;
Adafruit_ILI9341 tft(TFT_CS, TFT_DC, TFT_RST);

// ===== 3. 每焦耳筆數：電腦經序列埠送指令 =====
//   S<文字>               標題下方的狀態列，例如 SIDLE、SRUN V8b (FPGA)
//   B                     一輪開始：能量歸零
//   E<列>,<名稱>,<筆數>    一輪結束：用這段期間的能量算「筆/J」，顯示在螢幕底部第 <列> 行（0、1 或 2）
String cmdBuf;

void tftInit() {                   // 畫出固定的標題與欄位名稱
  SPI.begin(TFT_SCK, TFT_MISO, TFT_MOSI, TFT_CS);   // 明確指定 SPI 腳位
  tft.begin();
  tft.setRotation(1);              // 橫向 320 x 240
  tft.fillScreen(ILI9341_BLACK);
  tft.setTextSize(2);
  tft.setTextColor(ILI9341_CYAN);
  tft.setCursor(10, 8);
  tft.print("EdgeFHE Power Probe");
  const char* labels[] = {"Voltage", "Current", "Power", "Energy"};
  tft.setTextColor(ILI9341_LIGHTGREY);
  for (int k = 0; k < 4; k++) { tft.setCursor(10, 50 + k * 40); tft.print(labels[k]); }
}

void tftShow(float v, float i_mA, float p_mW, float e) {   // 更新四個數值
  char buf[16];
  tft.setTextSize(3);
  tft.setTextColor(ILI9341_WHITE, ILI9341_BLACK);   // 帶背景色：蓋掉舊數字，不閃爍
  snprintf(buf, sizeof buf, "%7.3f V ", v);           tft.setCursor(130, 45);  tft.print(buf);
  snprintf(buf, sizeof buf, "%7.1f mA", i_mA);        tft.setCursor(130, 85);  tft.print(buf);
  snprintf(buf, sizeof buf, "%7.3f W ", p_mW / 1000); tft.setCursor(130, 125); tft.print(buf);
  snprintf(buf, sizeof buf, "%7.1f J ", e);           tft.setCursor(130, 165); tft.print(buf);
}

void showState(const String& s) {                  // 標題下方：目前狀態（閒置或正在跑哪一版）
  char buf[26];
  snprintf(buf, sizeof buf, "%-25s", s.c_str());
  tft.setTextSize(2);
  tft.setTextColor(ILI9341_GREEN, ILI9341_BLACK);
  tft.setCursor(10, 27);
  tft.print(buf);
}

void showResult(int row, const String& label, double recPerJ) {   // 螢幕底部：名稱與每焦耳筆數
  char buf[32];
  snprintf(buf, sizeof buf, "%-8s %7.0f rec/J", label.c_str(), recPerJ);
  tft.setTextSize(2);
  tft.setTextColor(ILI9341_YELLOW, ILI9341_BLACK);
  tft.setCursor(10, 192 + row * 16);
  tft.print(buf);
}

void handleLine(const String& line) {
  if (line.startsWith("S")) {                          // 狀態列
    showState(line.substring(1));
    return;
  }
  if (line == "B") {                                   // 一輪開始
    ina228.resetAccumulators();
    Serial.println("# run begin");
    return;
  }
  if (line.startsWith("E")) {                          // 一輪結束：E<列>,<名稱>,<筆數>
    int c1 = line.indexOf(','), c2 = line.indexOf(',', c1 + 1);
    if (c1 < 0 || c2 < 0) return;
    int row = line.substring(1, c1).toInt();
    String label = line.substring(c1 + 1, c2);
    double records = line.substring(c2 + 1).toDouble();
    double e = K_V * K_I * ina228.readEnergy();        // 這一輪的能量（J）
    double rpj = e > 0 ? records / e : 0;
    Serial.printf("# result %s records=%.0f E_J=%.3f rec_per_J=%.1f\n", label.c_str(), records, e, rpj);
    showResult(row < 0 ? 0 : (row > 2 ? 2 : row), label, rpj);
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  tftInit();
  showState("State: --");
  Wire.begin(SDA_PIN, SCL_PIN);
  if (!ina228.begin(0x40, &Wire)) {
    Serial.println("# ERROR: INA228 not found. Check SDA/SCL/VCC/GND.");
    tft.setTextSize(2);
    tft.setTextColor(ILI9341_RED, ILI9341_BLACK);
    tft.setCursor(10, 215);
    tft.print("INA228 not found");
    while (true) delay(1000);
  }
  ina228.setShunt(0.015, 2.7);                 // R015 = 0.015 Ω，最大 2.7 A
  ina228.setADCRange(1);                       // ±40.96 mV 量程，解析度約 5 µA
  ina228.setMode(INA228_MODE_CONT_BUS_SHUNT);  // 連續量測電壓與電流
  ina228.setCurrentConversionTime(INA228_TIME_540_us);
  ina228.setVoltageConversionTime(INA228_TIME_540_us);
  ina228.setAveragingCount(INA228_COUNT_16);   // 每筆約 17 ms
  ina228.resetAccumulators();                  // 能量歸零
  Serial.println("# t_ms,V,I_mA,P_mW,E_J");
}

void loop() {
  static uint32_t last = 0, lastTft = 0;
  while (Serial.available()) {                 // 讀電腦送來的指令
    char c = Serial.read();
    if (c == 'r' && cmdBuf.length() == 0) {    // 單一字元 r：能量歸零（probe_log.py 使用）
      ina228.resetAccumulators();
      Serial.println("# energy reset");
    } else if (c == '\n') {
      cmdBuf.trim();
      handleLine(cmdBuf);
      cmdBuf = "";
    } else if (c != '\r' && cmdBuf.length() < 40) {
      cmdBuf += c;
    }
  }
  if (millis() - last < 100) return;          // 每 100 ms 輸出一筆
  last = millis();
  float v = K_V * ina228.getBusVoltage_V();                  // V
  float i = K_I * (ina228.getCurrent_mA() - I_OFFSET_mA);    // mA
  float p = v * i;                                           // mW
  float e = K_V * K_I * ina228.readEnergy();                 // J
  Serial.printf("%lu,%.4f,%.2f,%.1f,%.3f\n", (unsigned long)last, v, i, p, e);
  if (last - lastTft >= 500) { lastTft = last; tftShow(v, i, p, e); }   // 每 0.5 秒更新螢幕
}
