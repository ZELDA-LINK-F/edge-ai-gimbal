/* ============================================================
 * M3 固件：命令通信 + LX-16A 总线舵机控制
 *   野火 F103VET6 指南者
 *   USART1 (PA9/PA10) → CH340 → 电脑/Jetson
 *   USART2 (PA2/PA3)  → LX-16A 舵机（单总线，115200）
 *
 * 命令：
 *   PING             → PONG
 *   HELP             → 命令列表
 *   S<id>,<pos>      → 舵机运动，例: S1,500  (id=1, 位置0~1000)
 *   S<id>,<pos>,<ms> → 带时间，例: S1,500,800
 *   T                → 发送测试帧并回显十六进制
 * ============================================================ */
#include <stdint.h>

/* ---------------- 寄存器 ---------------- */
typedef struct { volatile uint32_t CR, CFGR, CIR, APB2RSTR, APB1RSTR, AHBENR, APB2ENR, APB1ENR, BDCR, CSR; } RCC_TypeDef;
#define RCC ((RCC_TypeDef *)0x40021000U)
#define FLASH_ACR (*(volatile uint32_t *)0x40022000U)
typedef struct { volatile uint32_t CRL, CRH, IDR, ODR, BSRR, BRR, LCKR; } GPIO_TypeDef;
#define GPIOA ((GPIO_TypeDef *)0x40010800U)
typedef struct { volatile uint32_t CTRL, LOAD, VAL, CALIB; } SysTick_TypeDef;
#define SYSTICK ((SysTick_TypeDef *)0xE000E010U)
typedef struct { volatile uint32_t SR, DR, BRR, CR1, CR2, CR3, GTPR; } USART_TypeDef;
#define USART1 ((USART_TypeDef *)0x40013800U)
#define USART2 ((USART_TypeDef *)0x40004400U)

#define RCC_CR_HSEON (1U<<16)
#define RCC_CR_HSERDY (1U<<17)
#define RCC_CR_PLLON (1U<<24)
#define RCC_CR_PLLRDY (1U<<25)
#define RCC_CFGR_SW_PLL (2U<<0)
#define RCC_CFGR_SWS_MASK (3U<<2)
#define RCC_CFGR_SWS_PLL (2U<<2)
#define RCC_CFGR_PLLSRC (1U<<16)
#define RCC_CFGR_PLLMULL9 (7U<<18)
#define RCC_CFGR_PPRE1_DIV2 (4U<<8)
#define RCC_APB2ENR_IOPAEN (1U<<2)
#define RCC_APB2ENR_USART1EN (1U<<14)
#define RCC_APB1ENR_USART2EN (1U<<17)
#define USART_SR_TXE (1U<<7)
#define USART_SR_TC  (1U<<6)
#define USART_SR_RXNE (1U<<5)
#define USART_CR1_RE (1U<<2)
#define USART_CR1_TE (1U<<3)
#define USART_CR1_UE (1U<<13)

/* ---------------- SysTick（1ms）---------------- */
static volatile uint32_t g_ms = 0;
void SysTick_Handler(void) { g_ms++; }
static void systick_init(void)
{
    SYSTICK->CTRL = 0;                 /* 先关 */
    SYSTICK->LOAD = 9000 - 1;          /* HCLK/8 = 9MHz -> 9000 ticks = 1ms */
    SYSTICK->VAL  = 0;
    SYSTICK->CTRL = 3;                 /* 使能 + 中断 + HCLK/8（bit2=0）*/
}
static uint32_t millis(void) { return g_ms; }

/* ---------------- 时钟 ---------------- */
static void clock_init(void)
{
    RCC->CR |= RCC_CR_HSEON;
    for (volatile uint32_t i = 0; i < 200000; i++)
        if (RCC->CR & RCC_CR_HSERDY) break;
    if (RCC->CR & RCC_CR_HSERDY) {
        FLASH_ACR = 0x12;
        RCC->CFGR = RCC_CFGR_PLLSRC | RCC_CFGR_PLLMULL9;
        RCC->CR |= RCC_CR_PLLON;
        while (!(RCC->CR & RCC_CR_PLLRDY)) { }
        RCC->CFGR |= RCC_CFGR_PPRE1_DIV2;
        RCC->CFGR |= RCC_CFGR_SW_PLL;
        while ((RCC->CFGR & RCC_CFGR_SWS_MASK) != RCC_CFGR_SWS_PLL) { }
    }
}

/* ---------------- GPIO / 串口 ---------------- */
static void gpio_usart_init(void)
{
    RCC->APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_USART1EN;
    RCC->APB1ENR |= RCC_APB1ENR_USART2EN;
    GPIOA->CRH = (GPIOA->CRH & ~(0xFU<<4))  | (0xBU<<4);
    GPIOA->CRH = (GPIOA->CRH & ~(0xFU<<8))  | (0x4U<<8);
    /* PA2 = USART2_TX : 默认推挽（发送时强驱动），发完切高阻 */
    GPIOA->CRL = (GPIOA->CRL & ~(0xFU<<8))  | (0xBU<<8);
    /* PA3 = USART2_RX : 输入【上拉】（给开漏总线提供上拉）*/
    GPIOA->CRL = (GPIOA->CRL & ~(0xFU<<12)) | (0x8U<<12);
    GPIOA->BSRR = (1U<<3);   /* ODR3=1 -> PA3 输入上拉 */
    USART1->BRR = 625;  USART1->CR1 = USART_CR1_UE|USART_CR1_TE|USART_CR1_RE;
    USART2->BRR = 312;  USART2->CR1 = USART_CR1_UE|USART_CR1_TE|USART_CR1_RE;
}
static void u1_putc(char c) { while (!(USART1->SR & USART_SR_TXE)) {} USART1->DR = (uint8_t)c; }
static void u1_puts(const char *s) { while (*s) u1_putc(*s++); }
static void u1_putu(uint32_t v) { char b[12]; int i=0; if(!v){u1_putc('0');return;} while(v){b[i++]='0'+(v%10);v/=10;} while(i)u1_putc(b[--i]); }
static void u1_puthex(uint8_t v) { const char *h="0123456789ABCDEF"; u1_putc(h[v>>4]); u1_putc(h[v&0xF]); }
static int  u1_getc(char *c) { if (USART1->SR & USART_SR_RXNE) { *c=(char)(USART1->DR&0xFF); return 1; } return 0; }
static void u2_putc(char c) { while (!(USART2->SR & USART_SR_TXE)) {} USART2->DR = (uint8_t)c; }

/* ---------------- LX-16A 舵机帧 ---------------- */
/* 帧格式: 55 55 ID LEN CMD P1 P2 ... CHECKSUM
 *   LEN = CMD+参数 的字节数
 *   CHECKSUM = ~(ID+LEN+CMD+参数) & 0xFF
 * 运动指令: CMD=1, 参数 = 位置(2字节小端) 时间(2字节小端) 速度(2字节小端)
 */
/* 把 PA2 设成推挽（强驱动）或高阻输入 */
static void pa2_drive(int on)
{
    if (on) GPIOA->CRL = (GPIOA->CRL & ~(0xFU<<8)) | (0xBU<<8);   /* 复用推挽 50MHz */
    else    GPIOA->CRL = (GPIOA->CRL & ~(0xFU<<8)) | (0x4U<<8);   /* 浮空输入 = 高阻 */
}

static void servo_send_frame(uint8_t id, uint8_t cmd, const uint8_t *params, uint8_t plen)
{
    uint8_t len = plen + 3;          /* ID(1) + CMD(1) + 参数 + 校验和(1)，官方规则：Length+3 = 整包长度 */
    uint16_t sum = 0;
    pa2_drive(1);                    /* 发送前：强驱动 */
    sum += id; sum += len; sum += cmd;
    for (uint8_t i = 0; i < plen; i++) sum += params[i];

    u2_putc(0x55); u2_putc(0x55);
    u2_putc((char)id);
    u2_putc((char)len);
    u2_putc((char)cmd);
    for (uint8_t i = 0; i < plen; i++) u2_putc((char)params[i]);
    u2_putc((char)((~sum) & 0xFF));

    /* 等最后一个字节完全移出（TC），再切高阻，避免和舵机应答抢总线 */
    uint32_t t0 = millis();
    while (!(USART2->SR & USART_SR_TC) && (millis() - t0) < 5) { }
    pa2_drive(0);
}

/* SERVO_MOVE_TIME_WRITE (指令1, 数据长度7)
 * 参数: 角度低八位, 角度高八位, 时间低八位, 时间高八位
 * 官方例子: 55 55 01 07 01 F4 01 E8 03 crc  (角度500, 时间1000ms)
 */
static void servo_move(uint8_t id, uint16_t pos, uint16_t time_ms)
{
    uint8_t p[4];
    p[0] = pos & 0xFF;     p[1] = pos >> 8;
    p[2] = time_ms & 0xFF; p[3] = time_ms >> 8;
    servo_send_frame(id, 1, p, 4);
    /* 回显完整帧（含校验和），方便验证 */
    uint8_t len = 4 + 3;
    uint16_t sum = id + len + 1;
    for (int i = 0; i < 4; i++) sum += p[i];
    u1_puts("TX: 5555");
    u1_puthex(id); u1_puthex(len); u1_puthex(1);
    for (int i = 0; i < 4; i++) u1_puthex(p[i]);
    u1_puthex((uint8_t)((~sum) & 0xFF));
    u1_puts("\r\n");
}

/* ---------------- 简单数字解析 ---------------- */
static uint32_t parse_u(const char *s, int *pos)
{
    uint32_t v = 0;
    while (s[*pos] >= '0' && s[*pos] <= '9') { v = v*10 + (s[*pos]-'0'); (*pos)++; }
    return v;
}

/* ---------------- 主程序 ---------------- */
int main(void)
{
    clock_init();
    gpio_usart_init();
    systick_init();

    u1_puts("\r\n==========================================\r\n");
    u1_puts(" STM32F103VET6 READY  (M3: servo control)\r\n");
    u1_puts(" PING | HELP | S<id>,<pos>[,<ms>] | T\r\n");
    u1_puts("==========================================\r\n");

    char line[80];
    int li = 0;
    char c;
    uint32_t last_beat = millis();
    uint32_t beat = 0;

    while (1) {
        while (u1_getc(&c)) {
            if (c == '\r' || c == '\n') {
                if (li > 0) {
                    line[li] = 0;
                    li = 0;

                    if (line[0]=='P' && line[1]=='I' && line[2]=='N' && line[3]=='G' && line[4]==0) {
                        u1_puts("PONG\r\n");
                    }
                    else if (line[0]=='H' && line[1]=='E' && line[2]=='L' && line[3]=='P' && line[4]==0) {
                        u1_puts("PING | HELP | S<id>,<pos>[,<ms>] | T\r\n");
                    }
                    else if (line[0]=='T' && line[1]==0) {
                        u1_puts("测试帧 S1,500,800:\r\n");
                        servo_move(1, 500, 800);
                    }
                    else if (line[0]=='I' && line[1]=='D') {
                        /* ID<旧>,<新> : 修改舵机 ID（掉电保存）*/
                        int i = 2;
                        uint32_t oldid = parse_u(line, &i);
                        if (line[i] == ',') i++;
                        uint32_t newid = parse_u(line, &i);
                        if (oldid == 0 || oldid > 253 || newid > 253) {
                            u1_puts("ERR: ID 0~253\r\n");
                        } else {
                            uint8_t p[1]; p[0] = (uint8_t)newid;
                            servo_send_frame((uint8_t)oldid, 13, p, 1);   /* SERVO_ID_WRITE */
                            u1_puts("ID_WRITE: 把 ID "); u1_putu(oldid);
                            u1_puts(" 改成 "); u1_putu(newid); u1_puts("（掉电保存）\r\n");
                        }
                    }
                    else if (line[0]=='L' && line[1]=='D') {
                        /* LD<id>,<0|1> : 0=卸载(无力矩) 1=装载(有力矩) SERVO_LOAD_OR_UNLOAD_WRITE */
                        int i = 2;
                        uint32_t id = parse_u(line, &i);
                        if (line[i] == ',') i++;
                        uint32_t v = parse_u(line, &i);
                        if (id == 0 || id > 253) { u1_puts("ERR id\r\n"); }
                        else {
                            uint8_t p[1]; p[0] = (uint8_t)(v ? 1 : 0);
                            servo_send_frame((uint8_t)id, 31, p, 1);
                            u1_puts("LOAD "); u1_putu(v ? 1 : 0); u1_puts(" -> ID "); u1_putu(id); u1_puts("\r\n");
                        }
                    }
                    else if (line[0]=='B' && line[1]=='A' && line[2]=='U' && line[3]=='D' && line[4]==0) {
                        /* 波特率扫描：找舵机在哪个波特率下应答 */
                        static const uint16_t brrs[9] = {7500, 3750, 1875, 1250, 625, 312, 156, 78, 72};
                        static const uint32_t bauds[9] = {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000};
                        u1_puts("波特率扫描（每个试 ID=1 读位置）\r\n");
                        for (int i = 0; i < 9; i++) {
                            USART2->BRR = brrs[i];
                            /* 清残留 */
                            uint32_t tq = millis(); 
                            while (millis()-tq < 3) { while (USART2->SR & USART_SR_RXNE) { (void)USART2->DR; } }
                            servo_send_frame(1, 28, 0, 0);
                            uint8_t buf[40]; int n = 0;
                            uint32_t t0 = millis();
                            while (millis() - t0 < 40) {
                                if (USART2->SR & USART_SR_RXNE) {
                                    if (n < 40) buf[n++] = (uint8_t)(USART2->DR & 0xFF);
                                }
                            }
                            /* 回声 = 6字节；真应答 >= 7 字节 */
                            if (n >= 7) {
                                u1_puts("  ★ ");  u1_putu(bauds[i]); u1_puts(" bps: ");
                                for (int k = 0; k < n && k < 12; k++) { u1_puthex(buf[k]); u1_putc(' '); }
                                u1_puts("\r\n");
                            } else {
                                u1_puts("  "); u1_putu(bauds[i]); u1_puts(" bps: 回声"); u1_putu(n); u1_puts("字节\r\n");
                            }
                        }
                        USART2->BRR = 312;   /* 恢复 115200 */
                        u1_puts("扫描结束（恢复 115200）\r\n");
                    }
                    else if (line[0]=='S' && line[1]=='C' && line[2]=='A' && line[3]=='N' && line[4]==0) {
                        /* 扫描 ID 1~20，找哪些舵机有应答 */
                        u1_puts("扫描 ID 1~20 ...\r\n");
                        for (uint8_t sid = 1; sid <= 20; sid++) {
                            while (USART2->SR & USART_SR_RXNE) { (void)USART2->DR; }
                            servo_send_frame(sid, 28, 0, 0);
                            uint8_t buf[40]; int n = 0;
                            uint32_t t0 = millis();
                            while (millis() - t0 < 30) {
                                if (USART2->SR & USART_SR_RXNE) {
                                    if (n < 40) buf[n++] = (uint8_t)(USART2->DR & 0xFF);
                                }
                            }
                            /* 忽略回声（回声长度 = 6 字节且第5字节=0x1C）*/
                            int is_echo = (n >= 2 && buf[0] == 0x55 && buf[1] == 0x55 && n <= 7 && buf[4] == 0x1C);
                            if (n > 0 && !is_echo) {
                                u1_puts("  ID="); u1_putu(sid); u1_puts(" <- ");
                                for (int k = 0; k < n; k++) { u1_puthex(buf[k]); u1_putc(' '); }
                                u1_puts("\r\n");
                            }
                        }
                        u1_puts("扫描结束\r\n");
                    }
                    else if (line[0]=='L' && line[1]=='O' && line[2]=='O' && line[3]=='P' && line[4]==0) {
                        /* LOOP: 回环自测 - PA2 发, PA3 收（两者都接在信号线上）*/
                        u1_puts("回环测试: 发送 0x55, 期待收到 0x55\r\n");
                        pa2_drive(1);
                        while (!(USART2->SR & USART_SR_TXE)) {}
                        USART2->DR = 0x55;
                        uint32_t t0 = millis();
                        while (!(USART2->SR & USART_SR_TC) && (millis()-t0) < 5) {}
                        int got = 0; uint8_t rb = 0;
                        t0 = millis();
                        while (millis() - t0 < 3) {
                            if (USART2->SR & USART_SR_RXNE) { rb = (uint8_t)(USART2->DR & 0xFF); got = 1; break; }
                        }
                        if (got) {
                            u1_puts("收到 0x"); u1_puthex(rb);
                            u1_puts(rb == 0x55 ? "  ✅ 接线正常！\r\n" : "  ⚠️ 收到但不是 0x55\r\n");
                        } else {
                            u1_puts("❌ 没收到 —— PA2/PA3 之间没有连通\r\n");
                        }
                    }
                    else if (line[0]=='R' || line[0]=='r') {
                        /* R<id> : 读舵机位置（CAN 指令 28），回显所有应答字节 */
                        int i = 1;
                        uint32_t id = parse_u(line, &i);
                        if (id == 0 || id > 253) {
                            u1_puts("ERR id 1~253\r\n");
                        } else {
                            /* 先清空残留 */
                            while (USART2->SR & USART_SR_RXNE) { (void)USART2->DR; }
                            servo_send_frame((uint8_t)id, 28, 0, 0);
                            /* 立刻收，绝不打印（打印会丢字节）*/
                            uint8_t buf[40];
                            int n = 0;
                            uint32_t t0 = millis();
                            while (millis() - t0 < 50) {
                                if (USART2->SR & USART_SR_RXNE) {
                                    if (n < 40) buf[n++] = (uint8_t)(USART2->DR & 0xFF);
                                }
                            }
                            /* 收完再打印 */
                            u1_puts("R"); u1_putu(id); u1_puts(" 收"); u1_putu(n); u1_puts("字节: ");
                            if (n == 0) u1_puts("(无应答)");
                            for (int k = 0; k < n; k++) { u1_puthex(buf[k]); u1_putc(' '); }
                            u1_puts("\r\n");
                        }
                    }
                    else if (line[0]=='S' || line[0]=='s') {
                        int i = 1;
                        uint32_t id = parse_u(line, &i);
                        if (line[i] == ',') i++;
                        uint32_t pos = parse_u(line, &i);
                        uint32_t ms = 0;
                        if (line[i] == ',') { i++; ms = parse_u(line, &i); }
                        if (id == 0 || (id > 253 && id != 254) || pos > 1000) {
                            u1_puts("ERR 参数: id 1~253(或254广播), pos 0~1000\r\n");
                        } else {
                            servo_move((uint8_t)id, (uint16_t)pos, (uint16_t)ms);
                            u1_puts("OK id=");
                            u1_putu(id); u1_puts(" pos="); u1_putu(pos);
                            u1_puts(" ms="); u1_putu(ms); u1_puts("\r\n");
                        }
                    }
                    else {
                        u1_puts("ERR unknown\r\n");
                    }
                }
            } else if (li < (int)sizeof(line) - 1) {
                line[li++] = c;
            }
        }

        if (millis() - last_beat >= 1000) {
            last_beat = millis();
            beat++;
            u1_puts("HELLO ");
            u1_putu(beat);
            u1_puts("\r\n");
        }
    }
}
