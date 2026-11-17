// Water Sentinel EDGE firmware v2 - the ESP32 runs the MSPC detector itself.
//
//  sensors -> median -> Mahalanobis (MSPC) -> EWMA -> persistence/hysteresis -> alarm
//          -> LED / buzzer, ThingSpeak (every 16 s), ntfy phone alert, USB "DATA," lines (app still works)
//
// Same maths as sentinel/edge.py (EdgeDetector). Model numbers come from edge_model.h:
//   py -m sentinel export-edge   (re-export and re-flash after every training)
// Secrets (Wi-Fi, ThingSpeak key, ntfy topic) live in secrets.h (git-ignored, see secrets.example.h).
//
// Wiring and pins are identical to firmware/water_sentinel (DS18B20 D4, TDS D32 + power D25, turbidity D33, pH D35).
// Libraries: OneWire, DallasTemperature. Wi-Fi/HTTPClient come with the ESP32 core. Wi-Fi must be 2.4 GHz.
//
// USB output (115200): DATA,ms,temp_c,tds_v,turb_v,ph_pin_v   <- the laptop app reads this
//                      # EDGE,...                              <- comment line for debugging, ignored by the app

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>
#include "edge_model.h"
#include "edge_types.h"
#include "secrets.h"

const int TEMP_PIN = 4, TDS_PIN = 32, TURB_PIN = 33, PH_PIN = 35;
const int TDS_POWER_PIN = 25;                 // TDS "+" on the S pin of row D25 (switched, keeps pH quiet)
const unsigned long TDS_EVERY_MS = 5000;
const unsigned long INTERVAL_MS = 1000;
const float DT_S = 1.0f;                      // nominal seconds between samples (detector timing is derived from it)
const int LED_PIN = 2;                        // on-board LED: on = alarm, fast blink = sensor fault
const int BUZZER_PIN = -1;                    // set a GPIO for an ACTIVE buzzer (HIGH = sound); -1 = none
const unsigned long UPLOAD_EVERY_MS = 16000;  // free ThingSpeak accepts 1 update per 15 s
const int FAULT_NOTIFY_S = 30;                // phone alert if a sensor fault lasts this long

// ---------------------------------------------------------------- sensors
OneWire oneWire(TEMP_PIN);
DallasTemperature tempSensor(&oneWire);
unsigned long lastSample = 0;
bool firstTemp = true;

float readVolts(int pin, int n = 32) {
  long sum = 0;
  for (int i = 0; i < n; i++) { sum += analogReadMilliVolts(pin); delayMicroseconds(300); }
  return sum / (float)n / 1000.0f;
}

void printValue(float v, int digits) {
  if (isnan(v)) Serial.print("nan"); else Serial.print(v, digits);
}

// ---------------------------------------------------------------- detector (EdgeDetector in C++)
const char* STATUS_NAME[4] = {"starting", "normal", "anomaly", "fault"};
const char* SENSOR_NAME[4] = {"temperature", "TDS", "turbidity", "pH"};


const int MAX_WIN = 31;
float winBuf[MAX_WIN][EDGE_N];
int winCount = 0, WIN = 5, PERSIST = 3;
float LAM = 0.3f;
bool ewmaInit = false, alarmState = false;
float ewmaVal = 0;
int aboveN = 0, belowN = 0;
bool featOn[4] = {false, false, false, false};

void detectorInit() {
  for (int i = 0; i < EDGE_N; i++) featOn[EDGE_FEAT[i]] = true;
  int win = (int)lroundf(EDGE_MEDIAN_S / DT_S);
  if (win < 1) win = 1;
  if (win >= 2 && win % 2 == 0) win++;
  if (win > MAX_WIN) win = MAX_WIN;
  WIN = win;
  PERSIST = (int)ceilf(EDGE_PERSIST_S / DT_S - 1e-6f);
  if (PERSIST < 1) PERSIST = 1;
  LAM = 1.0f - powf(1.0f - EDGE_EWMA_LAMBDA, DT_S);
}

float tdsPpm(float v, float t) {
  float tt = isnan(t) ? 25.0f : t;
  float vv = v - EDGE_TDS_OFFSET_V;
  if (vv < 0) vv = 0;
  float vc = vv / (1.0f + 0.02f * (tt - 25.0f));
  return (133.42f * vc * vc * vc - 255.86f * vc * vc + 857.39f * vc) * 0.5f * EDGE_TDS_K;
}

float phValue(float pinV) {
  if (!featOn[3] || fabsf(EDGE_PH_V2 - EDGE_PH_V1) < 1e-6f) return NAN;
  return EDGE_PH_P1 + (EDGE_PH_P2 - EDGE_PH_P1) / (EDGE_PH_V2 - EDGE_PH_V1) * (pinV * EDGE_PH_DIVIDER - EDGE_PH_V1);
}

const char* faultCode(float t, float tdsV, float turbV, float phV) {
  if (featOn[0] && (isnan(t) || t < -50 || t == 85.0f)) return "temp_missing";
  if (featOn[1] && (isnan(tdsV) || tdsV <= EDGE_TDS_DRY_V)) return "tds_dry";
  if (featOn[1] && tdsV >= EDGE_ADC_MAX_V) return "tds_saturated";
  if (featOn[2] && !isnan(turbV) && turbV >= EDGE_ADC_MAX_V) return "turb_saturated";
  if (featOn[2] && (isnan(turbV) || turbV <= EDGE_MIN_SIGNAL_V)) return "turb_missing";
  if (featOn[3] && !isnan(phV) && phV >= EDGE_ADC_MAX_V) return "ph_saturated";
  if (featOn[3] && (isnan(phV) || phV <= EDGE_MIN_SIGNAL_V)) return "ph_missing";
  return nullptr;
}

Result detectorUpdate(float t, float tdsV, float turbV, float phPinV) {
  Result r;
  memset(&r, 0, sizeof(r));
  for (int k = 0; k < 4; k++) r.vals[k] = NAN;
  r.alarm = alarmState;

  const char* fc = faultCode(t, tdsV, turbV, phPinV);
  if (fc) { winCount = 0; r.status = ST_FAULT; r.fault = fc; return r; }

  float v[4] = {t, tdsPpm(tdsV, t), turbV, phValue(phPinV)};
  for (int k = 0; k < 4; k++) r.vals[k] = v[k];
  r.hasVals = true;

  if (winCount == WIN) {                                   // slide the window by one sample
    memmove(winBuf[0], winBuf[1], sizeof(float) * EDGE_N * (WIN - 1));
    winCount = WIN - 1;
  }
  for (int i = 0; i < EDGE_N; i++) winBuf[winCount][i] = v[EDGE_FEAT[i]];
  winCount++;
  if (winCount < WIN) { r.status = ST_STARTING; return r; }

  float x[EDGE_N];
  for (int i = 0; i < EDGE_N; i++) {                       // median of each column (window is odd)
    float c[MAX_WIN];
    for (int k = 0; k < WIN; k++) c[k] = winBuf[k][i];
    for (int a = 1; a < WIN; a++) { float key = c[a]; int b = a - 1; while (b >= 0 && c[b] > key) { c[b + 1] = c[b]; b--; } c[b + 1] = key; }
    x[i] = c[WIN / 2];
    r.vals[EDGE_FEAT[i]] = x[i];                           // upload the smoothed value
  }
  float d[EDGE_N], s = 0;
  for (int i = 0; i < EDGE_N; i++) d[i] = x[i] - EDGE_MU[i];
  for (int i = 0; i < EDGE_N; i++) for (int j = 0; j < EDGE_N; j++) s += d[i] * EDGE_P[i][j] * d[j];
  ewmaVal = ewmaInit ? LAM * s + (1 - LAM) * ewmaVal : s;
  ewmaInit = true;
  float ratio = (ewmaVal - EDGE_MED) / (EDGE_THR - EDGE_MED);

  if (!alarmState) {
    aboveN = ratio > 1.0f ? aboveN + 1 : 0;
    if (aboveN >= PERSIST) { alarmState = true; belowN = 0; }
  } else {
    belowN = ratio < EDGE_RELEASE ? belowN + 1 : 0;
    if (belowN >= PERSIST) { alarmState = false; aboveN = 0; }
  }
  int top = 0; float topZ = 0;
  for (int i = 0; i < EDGE_N; i++) { float z = d[i] / EDGE_SD[i]; if (fabsf(z) > fabsf(topZ)) { topZ = z; top = i; } }
  r.status = alarmState ? ST_ANOMALY : ST_NORMAL;
  r.alarm = alarmState; r.scored = true; r.ratio = ratio; r.driver = EDGE_FEAT[top]; r.driverZ = topZ;
  return r;
}

// ---------------------------------------------------------------- uplink (own task: Wi-Fi never delays sampling)
QueueHandle_t upQ;
volatile bool wifiUp = false;
volatile int tsLast = -1;          // -1 nothing yet, 0 rejected, >0 entry id, -2 network/HTTP error
volatile int tsOk = 0, tsFail = 0, ntfyOk = 0;

bool sendThingSpeak(const UpMsg& m) {
  char body[300];
  int n = snprintf(body, sizeof(body), "api_key=%s", TS_WRITE_KEY);
  for (int i = 0; i < 8; i++) if (m.has[i]) n += snprintf(body + n, sizeof(body) - n, "&field%d=%.4f", i + 1, m.f[i]);
  WiFiClientSecure client; client.setInsecure();
  HTTPClient http; http.setTimeout(8000);
  if (!http.begin(client, "https://api.thingspeak.com/update")) return false;
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  int code = http.POST((uint8_t*)body, strlen(body));
  String resp = code > 0 ? http.getString() : String("");
  http.end();
  if (code != 200) { tsLast = -2; return false; }
  tsLast = resp.toInt();
  return tsLast > 0;
}

bool sendNtfy(const UpMsg& m) {
  if (strlen(NTFY_TOPIC) == 0) return true;
  String url = String(NTFY_SERVER) + "/" + NTFY_TOPIC;
  WiFiClientSecure client; client.setInsecure();
  HTTPClient http; http.setTimeout(8000);
  if (!http.begin(client, url)) return false;
  http.addHeader("Title", m.title);
  http.addHeader("Priority", m.urgent ? "urgent" : "default");
  http.addHeader("Tags", m.urgent ? "rotating_light" : "white_check_mark");
  int code = http.POST((uint8_t*)m.body, strlen(m.body));
  http.end();
  return code >= 200 && code < 300;
}

void uplinkTask(void*) {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long lastTry = millis();
  for (;;) {
    if (WiFi.status() == WL_CONNECTED) wifiUp = true;
    else {
      wifiUp = false;
      if (millis() - lastTry > 12000) { lastTry = millis(); WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASS); }
    }
    UpMsg m;
    if (xQueueReceive(upQ, &m, pdMS_TO_TICKS(500)) != pdTRUE) continue;
    if (m.kind == 1) {                                   // alerts wait a little for Wi-Fi, ThingSpeak rows do not
      for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) vTaskDelay(pdMS_TO_TICKS(250));
    }
    if (WiFi.status() != WL_CONNECTED) { if (m.kind == 0) { tsFail++; tsLast = -2; } continue; }
    if (m.kind == 0) { if (sendThingSpeak(m)) tsOk++; else tsFail++; }
    else if (sendNtfy(m)) ntfyOk++;
  }
}

void queueNtfy(const char* title, const char* body, bool urgent) {
  UpMsg m; memset(&m, 0, sizeof(m));
  m.kind = 1; m.urgent = urgent;
  snprintf(m.title, sizeof(m.title), "%s", title);
  snprintf(m.body, sizeof(m.body), "%s", body);
  xQueueSend(upQ, &m, 0);
}

// one window of results -> one ThingSpeak row (worst status, highest score), like edge_sim.py
int winWorstRank = -2, winWorstCode = 0, winPeakDriver = 0;
bool winAny = false, winHasPeak = false, winHasVals = false;
float winPeak = 0, winPeakZ = 0, winVals[4] = {NAN, NAN, NAN, NAN};
unsigned long lastUpload = 0;

void windowAdd(const Result& r) {
  winAny = true;
  int rank = r.status == ST_STARTING ? -1 : r.status;
  if (rank > winWorstRank) { winWorstRank = rank; winWorstCode = r.status; }
  if (r.scored && (!winHasPeak || r.ratio > winPeak)) {
    winHasPeak = true; winPeak = r.ratio; winPeakDriver = r.driver; winPeakZ = r.driverZ;
  }
  if (r.hasVals) { winHasVals = true; for (int k = 0; k < 4; k++) winVals[k] = r.vals[k]; }
}

void windowFlush() {
  UpMsg m; memset(&m, 0, sizeof(m));
  m.kind = 0;
  for (int k = 0; k < 4; k++) if (winHasVals && !isnan(winVals[k])) { m.f[k] = winVals[k]; m.has[k] = true; }
  if (winHasPeak) {
    m.f[4] = winPeak; m.has[4] = true;
    m.f[6] = winPeakDriver + 1; m.has[6] = true;
    m.f[7] = winPeakZ; m.has[7] = true;
  }
  m.f[5] = winWorstCode; m.has[5] = true;
  xQueueSend(upQ, &m, 0);
  winWorstRank = -2; winWorstCode = 0; winAny = winHasPeak = winHasVals = false; winPeak = 0;
}

// ---------------------------------------------------------------- main
int curStatus = ST_STARTING;
bool prevAlarm = false, bootSent = false;
int faultSecs = 0; bool faultSent = false;

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
  pinMode(LED_PIN, OUTPUT);
  if (BUZZER_PIN >= 0) { pinMode(BUZZER_PIN, OUTPUT); digitalWrite(BUZZER_PIN, LOW); }
  pinMode(TDS_POWER_PIN, OUTPUT);
  digitalWrite(TDS_POWER_PIN, LOW);
  tempSensor.begin();
  tempSensor.setWaitForConversion(false);
  tempSensor.requestTemperatures();
  detectorInit();
  upQ = xQueueCreate(6, sizeof(UpMsg));
  xTaskCreatePinnedToCore(uplinkTask, "uplink", 20480, nullptr, 1, nullptr, 0);
  Serial.println("# Water Sentinel EDGE firmware v2");
  Serial.printf("# model window=%d persist=%d lambda=%.3f features=%d\n", WIN, PERSIST, LAM, EDGE_N);
  Serial.println("# DATA,ms,temp_c,tds_v,turb_v,ph_pin_v");
}

void updateIndicators() {
  unsigned long ms = millis();
  bool led = false, buz = false;
  if (curStatus == ST_FAULT) led = (ms / 250) % 2;
  else if (prevAlarm) { led = true; buz = (ms / 500) % 2; }
  digitalWrite(LED_PIN, led);
  if (BUZZER_PIN >= 0) digitalWrite(BUZZER_PIN, buz);
}

void loop() {
  updateIndicators();
  unsigned long now = millis();
  if (now - lastSample < INTERVAL_MS) { delay(2); return; }
  lastSample = now;

  float t = tempSensor.getTempCByIndex(0);
  bool tempOk = (t != DEVICE_DISCONNECTED_C) && !(firstTemp && t == 85.0f);
  firstTemp = false;
  tempSensor.requestTemperatures();
  if (!tempOk) t = NAN;

  static float lastTdsV = NAN;
  static unsigned long lastTdsAt = 0;
  if (isnan(lastTdsV) || now - lastTdsAt >= TDS_EVERY_MS) {
    digitalWrite(TDS_POWER_PIN, HIGH);
    delay(150);
    lastTdsV = readVolts(TDS_PIN);
    digitalWrite(TDS_POWER_PIN, LOW);
    lastTdsAt = now;
    delay(600);
  }
  float tdsV = lastTdsV;
  float turbV = readVolts(TURB_PIN);
  float phV = readVolts(PH_PIN, 128);

  Serial.print("DATA,");
  Serial.print(now);  Serial.print(",");
  printValue(t, 2);   Serial.print(",");
  printValue(tdsV, 4); Serial.print(",");
  printValue(turbV, 4); Serial.print(",");
  printValue(phV, 4); Serial.println();

  Result r = detectorUpdate(t, tdsV, turbV, phV);
  curStatus = r.status;
  windowAdd(r);

  if (r.alarm && !prevAlarm) {
    char b[120];
    snprintf(b, sizeof(b), "Abnormal water detected by the board (%s moved most, score %.2f).", SENSOR_NAME[r.driver], r.ratio);
    queueNtfy("Water Sentinel: anomaly", b, true);
  } else if (!r.alarm && prevAlarm) {
    queueNtfy("Water Sentinel: back to normal", "Water looks normal again.", false);
  }
  prevAlarm = r.alarm;

  if (r.status == ST_FAULT) {
    if (++faultSecs >= FAULT_NOTIFY_S && !faultSent) {
      char b[100]; snprintf(b, sizeof(b), "Sensor problem on the board: %s. Check the probe or cable.", r.fault);
      queueNtfy("Water Sentinel: sensor fault", b, false);
      faultSent = true;
    }
  } else { faultSecs = 0; faultSent = false; }

  if (!bootSent && wifiUp) { queueNtfy("Water Sentinel: board online", "The board is on Wi-Fi and running the MSPC model.", false); bootSent = true; }
  if (winAny && now - lastUpload >= UPLOAD_EVERY_MS) { lastUpload = now; windowFlush(); }

  if (isnan(r.ratio) || !r.scored)
    Serial.printf("# EDGE,%s,-,%s,wifi=%d,ts=%d/%d(last %d),ntfy=%d\n", STATUS_NAME[r.status], r.fault ? r.fault : "-", (int)wifiUp, (int)tsOk, (int)tsFail, (int)tsLast, (int)ntfyOk);
  else
    Serial.printf("# EDGE,%s,%.3f,%s,wifi=%d,ts=%d/%d(last %d),ntfy=%d\n", STATUS_NAME[r.status], r.ratio, SENSOR_NAME[r.driver], (int)wifiUp, (int)tsOk, (int)tsFail, (int)tsLast, (int)ntfyOk);
}
