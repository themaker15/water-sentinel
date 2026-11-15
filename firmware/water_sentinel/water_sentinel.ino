// Water Sentinel firmware v1 - streams raw sensor readings once per second.
// All calibration (TDS offset/K, pH two-point, divider ratio) lives in the PC app, so you
// never need to re-upload this sketch to recalibrate.
//
// Board: ESP32 Dev Module on the expansion board (V-pin jumper at 3.3V)
// Libraries: OneWire, DallasTemperature (Arduino Library Manager)
//
// Wiring (expansion board rows: S = signal, V = power, G = ground)
//   DS18B20   row D4  : DAT->S  VCC->V  GND->G
//   TDS       row D32 : A->S    + ->V   - ->G      (or + -> GPIO25, see TDS_POWER_PIN)
//   Turbidity row D33 : OUT->S  VCC->V  GND->G     (3.3V)
//   pH board  row D35 : Po -> 10k -> S, and S -> 10k -> G   (divider), V+ -> 5V, G -> G
//                       Never connect Po straight to the ESP32 when the pH board runs on 5V.
//   Without a pH board, D35 just floats; the app ignores it unless "use pH" is on.
//
// Output line (115200 baud):
//   DATA,ms,temp_c,tds_v,turb_v,ph_pin_v
// tds_v / turb_v / ph_pin_v are the voltages at the ESP32 pins (0-3.3 V).

#include <OneWire.h>
#include <DallasTemperature.h>

const int TEMP_PIN = 4;
const int TDS_PIN  = 32;
const int TURB_PIN = 33;
const int PH_PIN   = 35;

// pH and TDS probes in the same water can disturb each other (both put current through the water).
// Optional fix: power the TDS board from a GPIO and switch it off while pH is read.
// Wire TDS "+" to the S pin of row D25 and set TDS_POWER_PIN = 25. Keep -1 to power it from V.
const int TDS_POWER_PIN = -1;

const unsigned long INTERVAL_MS = 1000;

OneWire oneWire(TEMP_PIN);
DallasTemperature tempSensor(&oneWire);
unsigned long lastSample = 0;
bool firstTemp = true;

float readVolts(int pin, int n = 32) {
  long sum = 0;
  for (int i = 0; i < n; i++) {
    sum += analogReadMilliVolts(pin);
    delayMicroseconds(300);
  }
  return sum / (float)n / 1000.0;
}

void printValue(float v, int digits) {
  if (isnan(v)) Serial.print("nan");
  else Serial.print(v, digits);
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
  if (TDS_POWER_PIN >= 0) {
    pinMode(TDS_POWER_PIN, OUTPUT);
    digitalWrite(TDS_POWER_PIN, LOW);
  }
  tempSensor.begin();
  tempSensor.setWaitForConversion(false);   // non-blocking: ask now, read next second
  tempSensor.requestTemperatures();
  Serial.println("# Water Sentinel firmware v1");
  Serial.println("# DATA,ms,temp_c,tds_v,turb_v,ph_pin_v");
}

void loop() {
  unsigned long now = millis();
  if (now - lastSample < INTERVAL_MS) return;
  lastSample = now;

  // temperature requested one second ago (DS18B20 needs ~750 ms at 12-bit)
  float t = tempSensor.getTempCByIndex(0);
  bool tempOk = (t != DEVICE_DISCONNECTED_C) && !(firstTemp && t == 85.0);
  firstTemp = false;
  tempSensor.requestTemperatures();

  float tdsV;
  if (TDS_POWER_PIN >= 0) {
    digitalWrite(TDS_POWER_PIN, HIGH);
    delay(150);                       // let the TDS board settle
    tdsV = readVolts(TDS_PIN);
    digitalWrite(TDS_POWER_PIN, LOW);
    delay(80);                        // let the water relax before pH is read
  } else {
    tdsV = readVolts(TDS_PIN);
  }
  float turbV = readVolts(TURB_PIN);
  float phV = readVolts(PH_PIN);

  Serial.print("DATA,");
  Serial.print(now);            Serial.print(",");
  printValue(tempOk ? t : NAN, 2); Serial.print(",");
  printValue(tdsV, 4);          Serial.print(",");
  printValue(turbV, 4);         Serial.print(",");
  printValue(phV, 4);           Serial.println();
}
