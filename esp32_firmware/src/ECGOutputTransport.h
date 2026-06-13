#pragma once

#include <Arduino.h>

#include "config.h"

class ECGOutputTransport {
 public:
  ECGOutputTransport();

  void begin();
  Stream& stream();

 private:
#if !ECG_OUTPUT_USB
  HardwareSerial uart_;
#endif
};
