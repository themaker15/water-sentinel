// Types shared by the sketch (kept in a header so the Arduino auto-prototypes can see them).
#pragma once
#include <stdint.h>

enum { ST_STARTING = 0, ST_NORMAL = 1, ST_ANOMALY = 2, ST_FAULT = 3 };

// One MSPC model, padded to 4 sensors. Values come from edge_model.h (py -m sentinel export-edge).
struct EdgeModel {
  int n;                    // sensors used (3 or 4)
  int feat[4];              // sensor slot per input: 0 temp, 1 tds, 2 turb, 3 ph
  float mu[4], sd[4];
  float P[4][4];            // inverse covariance
  float thr, med, median_s, ewma_lambda, persist_s, release;
};

const int MAX_WIN = 31;

// The running state of one detector (one per model).
struct Det {
  const EdgeModel* m;
  float buf[MAX_WIN][4];
  int count, win, persist;
  float lam, ewma;
  bool ewmaInit, alarm;
  int above, below;
  bool featOn[4];
};

struct Result {
  int status;
  bool alarm, scored;
  float ratio, driverZ;
  int driver;               // sensor slot 0 temp, 1 tds, 2 turb, 3 ph
  const char* fault;
};

struct UpMsg {
  uint8_t kind;             // 0 = ThingSpeak, 1 = ntfy
  float f[8]; bool has[8];
  bool urgent;
  char title[48], body[160];
};

// one ThingSpeak window per model: worst status and highest score
struct WinAcc { int worstRank, worstCode, peakDriver; bool hasPeak; float peak; };
