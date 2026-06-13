#include "ECGOutputTransport.h"

ECGOutputTransport::ECGOutputTransport()
#if !ECG_OUTPUT_USB
    : uart_(Config::ECG_UART_NUM)
#endif
{}

void ECGOutputTransport::begin() {
#if !ECG_OUTPUT_USB
  uart_.begin(Config::ECG_UART_BAUD,
              SERIAL_8N1,
              Config::ECG_UART_RX_PIN,
              Config::ECG_UART_TX_PIN);
#endif
}

Stream& ECGOutputTransport::stream() {
#if ECG_OUTPUT_USB
  return Serial;
#else
  return uart_;
#endif
}
