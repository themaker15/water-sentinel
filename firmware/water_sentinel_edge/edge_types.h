// Types shared by the sketch (kept in a header so the Arduino auto-prototypes can see them).
#pragma once

enum { ST_STARTING = 0, ST_NORMAL = 1, ST_ANOMALY = 2, ST_FAULT = 3 };
struct Result {
  int status;
  bool alarm, scored, hasVals;
  float ratio, driverZ;
  int driver;               // sensor slot 0 temp, 1 tds, 2 turb, 3 ph
  float vals[4];            // temp C, TDS ppm, turbidity V, pH (NAN = not available)
  const char* fault;
};


struct UpMsg {
  uint8_t kind;             // 0 = ThingSpeak, 1 = ntfy
  float f[8]; bool has[8];
  bool urgent;
  char title[48], body[160];
};
