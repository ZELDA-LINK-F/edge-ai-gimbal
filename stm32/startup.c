/* STM32F103VET6 启动文件 */
#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
extern int main(void);
void Reset_Handler(void);
void Default_Handler(void);
void SysTick_Handler(void);

__attribute__((section(".isr_vector"), used))
void (* const g_vectors[])(void) = {
    (void (*)(void))(&_estack),
    Reset_Handler,      /* Reset */
    Default_Handler,    /* NMI */
    Default_Handler,    /* HardFault */
    Default_Handler,    /* MemManage */
    Default_Handler,    /* BusFault */
    Default_Handler,    /* UsageFault */
    0, 0, 0, 0,
    Default_Handler,    /* SVCall */
    Default_Handler,    /* DebugMon */
    0,
    Default_Handler,    /* PendSV */
    SysTick_Handler,    /* SysTick */
};

void Default_Handler(void) { while (1) { } }

void Reset_Handler(void)
{
    uint32_t *src = &_sidata;
    uint32_t *dst = &_sdata;
    while (dst < &_edata) { *dst++ = *src++; }
    dst = &_sbss;
    while (dst < &_ebss) { *dst++ = 0; }
    main();
    while (1) { }
}
