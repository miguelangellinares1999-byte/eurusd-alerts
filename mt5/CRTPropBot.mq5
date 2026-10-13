//+------------------------------------------------------------------+
//| CRTPropBot.mq5                                                    |
//| Cartera CRT H4 -> entrada M15 de backtest/crt_funded.py para      |
//| cuentas fondeadas (pensado para The5%ers High Stakes 5k y para    |
//| probarlo antes en BlackBull). Configuraciones validadas fuera de  |
//| muestra (2023-2026):                                              |
//|  NAS100  todas las velas  sesgo D1    FVG  TP 1R                  |
//|  SPX500  Londres          sesgo EMA50 MSS  TP 2R + BE             |
//|  US30    Londres          sesgo D1    MSS  TP 2R                  |
//|  GBPUSD  Londres + NY     sesgo EMA50 FVG  TP 2R + BE             |
//|  XAUUSD  NY               sesgo prem. MSS  TP extremo de A (CRT)  |
//|                                                                   |
//| Setup H4 (cortos; largos simétricos): la vela B barre el máximo   |
//|  de A y cierra dentro de A. SL = máximo de B + colchón, objetivo  |
//|  CRT = mínimo de A. La vela C es la siguiente; Londres = C de las |
//|  01:00 NY, NY = C de las 09:00 NY.                                |
//| FVG: límite en el borde del primer FVG M15 a favor dentro de la   |
//|  ventana (1 vela H4; 2 si no hay filtro de sesión). Se cancela si |
//|  el precio toca el objetivo antes de llenarse o acaba la ventana. |
//| MSS: al cierre de una M15 por debajo del mínimo de las 4          |
//|  anteriores, entrada a mercado.                                   |
//| Antes de entrar se anula el setup si una M15 toca SL u objetivo.  |
//| Salida por tiempo a las 24 h. Una operación por símbolo.          |
//|                                                                   |
//| Protección de la cuenta (desactivada en InpVerifyMode):           |
//|  - riesgo fijo InpRiskPct del saldo inicial (0,5%), lote          |
//|    redondeado hacia abajo: nunca se arriesga más;                 |
//|  - pérdida diaria medida desde max(saldo, equity) a medianoche    |
//|    del servidor (regla de The5%ers), contando flotante y riesgo   |
//|    abierto; se usa solo InpDailyGuard del límite y se cierra todo |
//|    al 90%;                                                        |
//|  - DD máximo estático: no se abre nada que pueda llevar la equity |
//|    a menos de InpDDMarginPct por encima del suelo; cierre total y |
//|    parada definitiva al tocar el suelo + 0,2%;                    |
//|  - noticias de alto impacto (calendario de MT5): sin entradas ni  |
//|    órdenes pendientes InpNewsMinutes antes y después;             |
//|  - viernes: sin entradas desde InpFridayStopNY y cierre total a   |
//|    InpFridayCloseNY (no se mantiene nada el fin de semana);       |
//|  - objetivo de fase (InpTargetPct > 0): al alcanzarlo se cierra   |
//|    todo y el bot se detiene.                                      |
//| Operaciones cerradas -> Common\Files\CRTPropBot_trades.csv        |
//+------------------------------------------------------------------+
#property copyright "eurusd-alerts"
#property version   "1.00"
#property description "Cartera CRT H4/M15 (NAS100, SPX500, US30, GBPUSD, XAUUSD) para cuentas fondeadas"

#include <Trade/Trade.mqh>

enum EBias  { BIAS_NONE, BIAS_D1, BIAS_EMA, BIAS_PREM };
enum EEntry { ENTRY_FVG, ENTRY_MSS };
enum ETp    { TP_CRT, TP_1R, TP_2R };

input group "Cartera (nombres exactos de Observación del mercado)"
input bool   InpUseNAS   = true;      // NAS100: activar
input string InpSymNAS   = "NAS100";  // NAS100: símbolo
input double InpBufNAS   = 2.0;       // NAS100: colchón del SL (precio)
input bool   InpUseSPX   = true;      // SPX500: activar
input string InpSymSPX   = "SPX500";  // SPX500: símbolo
input double InpBufSPX   = 0.6;       // SPX500: colchón del SL
input bool   InpUseUS30  = true;      // US30: activar
input string InpSymUS30  = "US30";    // US30: símbolo
input double InpBufUS30  = 3.0;       // US30: colchón del SL
input bool   InpUseGBP   = true;      // GBPUSD: activar
input string InpSymGBP   = "GBPUSDp"; // GBPUSD: símbolo
input double InpBufGBP   = 0.00015;   // GBPUSD: colchón del SL
input bool   InpUseXAU   = true;      // XAUUSD: activar
input string InpSymXAU   = "XAUUSDp"; // XAUUSD: símbolo
input double InpBufXAU   = 0.30;      // XAUUSD: colchón del SL

input group "Riesgo"
input double InpRiskPct  = 0.5;       // Riesgo por operación (% del saldo inicial)

input group "Reglas de la cuenta"
input double InpInitialBalance = 0;   // Saldo inicial de la cuenta (0 = saldo al arrancar)
input double InpTargetPct      = 0;   // Objetivo de fase % (0 = cuenta fondeada, sin objetivo)
input double InpMaxDDPct       = 10;  // Drawdown máximo % (estático, sobre el saldo inicial)
input double InpDailyPct       = 5;   // Pérdida diaria máxima %
input bool   InpDailyFromMax   = true;// Pérdida diaria desde max(saldo, equity) a medianoche (The5%ers)
input double InpDailyGuard     = 0.8; // Fracción del límite diario que se puede arriesgar
input double InpDDMarginPct    = 1.0; // Margen sobre el suelo del DD que nunca se arriesga %

input group "Noticias y fin de semana"
input bool   InpNewsFilter     = true;  // Bloquear entradas cerca de noticias de alto impacto
input int    InpNewsMinutes    = 5;     // Minutos antes y después de la noticia
input bool   InpFridayClose    = true;  // No mantener posiciones el fin de semana
input int    InpFridayStopNY   = 1200;  // Viernes: sin entradas nuevas desde (HHMM hora NY)
input int    InpFridayCloseNY  = 1630;  // Viernes: cierre total a las (HHMM hora NY)

input group "Ejecución"
input int    InpServerMinusNY  = 7;        // Horas que el servidor va por delante de Nueva York
input long   InpMagic          = 20261013; // Magic base (NAS +1, SPX +2, US30 +3, GBP +4, XAU +5)
input int    InpDeviationPts   = 50;       // Desviación máxima en órdenes a mercado (puntos)
input bool   InpVerifyMode     = false;    // Verificación: riesgo fijo, sin reglas, noticias ni viernes
input bool   InpAllowReal      = false;    // Permitir operar en cuenta REAL (cuentas de fondeo reales)

struct Slot
{
   string   name;
   string   sym;
   double   buf;
   int      hours[2];   // hora NY de inicio de la vela C permitida (-1 = sin uso)
   int      window;     // velas H4 de ventana de entrada
   int      bias;
   int      entry;
   int      tp;
   bool     be;
   long     magic;
   int      ema;
   datetime lastH4;
   datetime lastM15;
};

struct Setup
{
   bool     on;
   int      slot;
   int      dir;
   double   sl, tgt, entry;
   datetime c, end;
   ulong    ticket;
};

CTrade trade;
Slot   g_s[];
Setup  g_set[];
double g_init = 0;
double g_dayBase = 0;
int    g_day = -1;
bool   g_tester = false;
const string CSV_NAME = "CRTPropBot_trades.csv";

// noticias: caché por símbolo
datetime g_newsAt[];
bool     g_newsBlocked[];

// entradas abiertas, para calcular R al cerrar
long     g_posId[];
double   g_posEntry[], g_posSl[];
datetime g_posTime[];
int      g_posDir[];
long     g_posMagic[];

string GV(string key) { return "CRT_" + (string)InpMagic + "_" + key; }

int SlotOfMagic(long magic)
{
   for(int i = 0; i < ArraySize(g_s); i++) if(g_s[i].magic == magic) return i;
   return -1;
}

int DateKey(datetime t)
{
   MqlDateTime d;
   TimeToStruct(t, d);
   return d.year * 10000 + d.mon * 100 + d.day;
}

datetime DayStart(datetime t)
{
   MqlDateTime d;
   TimeToStruct(t, d);
   d.hour = 0; d.min = 0; d.sec = 0;
   return StructToTime(d);
}

void NYNow(int &weekday, int &hhmm)
{
   MqlDateTime d;
   TimeToStruct(TimeTradeServer() - InpServerMinusNY * 3600, d);
   weekday = d.day_of_week;
   hhmm = d.hour * 100 + d.min;
}

double NormPrice(string s, double p)
{
   double tick = SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE);
   if(tick > 0) p = MathRound(p / tick) * tick;
   return NormalizeDouble(p, (int)SymbolInfoInteger(s, SYMBOL_DIGITS));
}

double LossMoney(string s, ENUM_ORDER_TYPE type, double vol, double open, double sl)
{
   double p = 0;
   if(!OrderCalcProfit(type, s, vol, open, sl, p)) return -1;
   return MathAbs(p);
}

void AddSlot(string name, string sym, double buf, int h1, int h2, int window, int bias, int entry, int tp, bool be, int k)
{
   int n = ArraySize(g_s);
   ArrayResize(g_s, n + 1);
   g_s[n].name = name; g_s[n].sym = sym; g_s[n].buf = buf;
   g_s[n].hours[0] = h1; g_s[n].hours[1] = h2;
   g_s[n].window = window; g_s[n].bias = bias; g_s[n].entry = entry; g_s[n].tp = tp; g_s[n].be = be;
   g_s[n].magic = InpMagic + k;
   g_s[n].ema = INVALID_HANDLE;
   g_s[n].lastH4 = 0; g_s[n].lastM15 = 0;
}

//+------------------------------------------------------------------+
//| Riesgo                                                            |
//+------------------------------------------------------------------+
//--- riesgo abierto: posiciones hasta su SL + órdenes pendientes completas
double OpenRisk()
{
   double risk = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || SlotOfMagic(PositionGetInteger(POSITION_MAGIC)) < 0) continue;
      double sl = PositionGetDouble(POSITION_SL);
      if(sl <= 0) continue;
      bool buy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      if(buy ? sl >= open : sl <= open) continue;  // en break-even o mejor
      risk += MathMax(LossMoney(PositionGetString(POSITION_SYMBOL), buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL,
                                PositionGetDouble(POSITION_VOLUME), open, sl), 0);
   }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong tk = OrderGetTicket(i);
      if(tk == 0 || SlotOfMagic(OrderGetInteger(ORDER_MAGIC)) < 0) continue;
      ENUM_ORDER_TYPE t = OrderGetInteger(ORDER_TYPE) == ORDER_TYPE_BUY_LIMIT ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      risk += MathMax(LossMoney(OrderGetString(ORDER_SYMBOL), t, OrderGetDouble(ORDER_VOLUME_CURRENT),
                                OrderGetDouble(ORDER_PRICE_OPEN), OrderGetDouble(ORDER_SL)), 0);
   }
   return risk;
}

double AllowedRisk()
{
   double risk = InpRiskPct / 100.0 * g_init;
   if(InpVerifyMode) return risk;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   double open = OpenRisk();
   double daily = InpDailyGuard * InpDailyPct / 100.0 * g_init - (g_dayBase - eq) - open;
   double dd = eq - open - g_init * (1 - InpMaxDDPct / 100.0 + InpDDMarginPct / 100.0);
   return MathMin(risk, MathMin(daily, dd));
}

double VolumeFor(string s, ENUM_ORDER_TYPE type, double entry, double sl, double riskMoney)
{
   double perLot = LossMoney(s, type, 1.0, entry, sl);
   if(perLot <= 0 || riskMoney <= 0) return 0;
   double step = SymbolInfoDouble(s, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(s, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(s, SYMBOL_VOLUME_MAX);
   double vol = MathFloor(riskMoney / perLot / step + 1e-9) * step;
   double margin = 0;
   if(OrderCalcMargin(type, s, 1.0, entry, margin) && margin > 0)
      vol = MathMin(vol, MathFloor(0.5 * AccountInfoDouble(ACCOUNT_MARGIN_FREE) / margin / step) * step);
   if(vol < vmin)
   {
      PrintFormat("%s: el riesgo %.2f no llega al lote mínimo (%.2f por lote): no se opera", s, riskMoney, perLot);
      return 0;
   }
   return NormalizeDouble(MathMin(vol, vmax), 8);
}

//+------------------------------------------------------------------+
//| Noticias, viernes y reglas                                        |
//+------------------------------------------------------------------+
bool HighImpactNear(string cur)
{
   if(cur == "") return false;
   MqlCalendarValue v[];
   datetime now = TimeTradeServer();
   if(!CalendarValueHistory(v, now - InpNewsMinutes * 60, now + InpNewsMinutes * 60, NULL, cur)) return false;
   for(int i = 0; i < ArraySize(v); i++)
   {
      MqlCalendarEvent ev;
      if(CalendarEventById(v[i].event_id, ev) && ev.importance == CALENDAR_IMPORTANCE_HIGH) return true;
   }
   return false;
}

bool NewsBlocked(int i)
{
   if(!InpNewsFilter || InpVerifyMode || g_tester) return false;
   datetime now = TimeTradeServer();
   if(now - g_newsAt[i] < 20) return g_newsBlocked[i];
   string s = g_s[i].sym;
   string base = SymbolInfoString(s, SYMBOL_CURRENCY_BASE), prof = SymbolInfoString(s, SYMBOL_CURRENCY_PROFIT);
   bool b = HighImpactNear("USD") || (base != "USD" && HighImpactNear(base)) || (prof != "USD" && prof != base && HighImpactNear(prof));
   if(b && !g_newsBlocked[i]) PrintFormat("%s: noticia de alto impacto en +-%d min, sin entradas", g_s[i].name, InpNewsMinutes);
   g_newsAt[i] = now;
   g_newsBlocked[i] = b;
   return b;
}

bool FridayStop()
{
   if(!InpFridayClose || InpVerifyMode) return false;
   int wd, hm;
   NYNow(wd, hm);
   return (wd == 5 && hm >= InpFridayStopNY) || wd == 6 || (wd == 0 && hm < 1700);
}

bool FridayClose()
{
   if(!InpFridayClose || InpVerifyMode) return false;
   int wd, hm;
   NYNow(wd, hm);
   return wd == 5 && hm >= InpFridayCloseNY;
}

void DeleteSetup(int k)
{
   if(g_set[k].ticket != 0 && OrderSelect(g_set[k].ticket)) trade.OrderDelete(g_set[k].ticket);
   g_set[k].ticket = 0;
   g_set[k].on = false;
}

void DropSetups(int slot)
{
   for(int k = 0; k < ArraySize(g_set); k++) if(g_set[k].on && g_set[k].slot == slot) DeleteSetup(k);
}

void CloseAll(string why)
{
   for(int k = 0; k < ArraySize(g_set); k++) if(g_set[k].on) DeleteSetup(k);
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong tk = OrderGetTicket(i);
      if(tk != 0 && SlotOfMagic(OrderGetInteger(ORDER_MAGIC)) >= 0) trade.OrderDelete(tk);
   }
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk != 0 && SlotOfMagic(PositionGetInteger(POSITION_MAGIC)) >= 0) trade.PositionClose(tk);
   }
   Print("Cierre total: ", why);
}

bool CheckRules(int today)
{
   if(today != g_day)
   {
      g_day = today;
      string k = GV("daybase_" + (string)today);
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      if(!GlobalVariableCheck(k)) GlobalVariableSet(k, InpDailyFromMax ? MathMax(bal, AccountInfoDouble(ACCOUNT_EQUITY)) : bal);
      g_dayBase = GlobalVariableGet(k);
   }
   if(InpVerifyMode) return true;
   if(GlobalVariableCheck(GV("finished"))) return false;
   if(GlobalVariableGet(GV("halt_day")) == today) return false;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(InpTargetPct > 0 && eq >= g_init * (1 + InpTargetPct / 100.0))
   {
      CloseAll("objetivo de fase alcanzado");
      GlobalVariableSet(GV("finished"), 1);
      Alert("CRTPropBot: OBJETIVO ALCANZADO. El bot deja de operar.");
      return false;
   }
   if(eq <= g_init * (1 - InpMaxDDPct / 100.0 + 0.002))
   {
      CloseAll("drawdown máximo");
      GlobalVariableSet(GV("finished"), -1);
      Alert("CRTPropBot: DRAWDOWN MÁXIMO. El bot deja de operar.");
      return false;
   }
   if(g_dayBase - eq >= 0.9 * InpDailyPct / 100.0 * g_init)
   {
      CloseAll("cerca del límite diario");
      GlobalVariableSet(GV("halt_day"), today);
      Alert("CRTPropBot: cerca del límite de pérdida diaria. Sin operar hasta mañana.");
      return false;
   }
   if(FridayClose())
   {
      if(GlobalVariableGet(GV("friday")) != today) { CloseAll("viernes"); GlobalVariableSet(GV("friday"), today); }
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
//| Posiciones                                                        |
//+------------------------------------------------------------------+
ulong FindPosition(int i)
{
   for(int p = PositionsTotal() - 1; p >= 0; p--)
   {
      ulong tk = PositionGetTicket(p);
      if(tk != 0 && PositionGetInteger(POSITION_MAGIC) == g_s[i].magic && PositionGetString(POSITION_SYMBOL) == g_s[i].sym) return tk;
   }
   return 0;
}

bool ManagePosition(int i)
{
   ulong tk = FindPosition(i);
   if(tk == 0) return false;
   DropSetups(i);  // una operación por símbolo: el resto de setups se anulan
   string s = g_s[i].sym;
   trade.SetExpertMagicNumber(g_s[i].magic);
   if(TimeTradeServer() - (datetime)PositionGetInteger(POSITION_TIME) >= 24 * 3600)
   {
      trade.PositionClose(tk);
      PrintFormat("%s: cierre por tiempo (24 h)", g_s[i].name);
      return true;
   }
   if(!g_s[i].be) return true;
   bool buy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
   double open = PositionGetDouble(POSITION_PRICE_OPEN), sl = PositionGetDouble(POSITION_SL);
   double risk = buy ? open - sl : sl - open;  // > 0 mientras el SL no se ha movido
   if(risk <= 0) return true;
   double bid = SymbolInfoDouble(s, SYMBOL_BID);
   if(buy ? bid >= open + risk : bid <= open - risk)
      if(trade.PositionModify(tk, NormPrice(s, open), PositionGetDouble(POSITION_TP)))
         PrintFormat("%s: SL a break-even (%.5f)", g_s[i].name, open);
   return true;
}

//+------------------------------------------------------------------+
//| Setups CRT                                                        |
//+------------------------------------------------------------------+
void NewSetup(int i, datetime c)
{
   string s = g_s[i].sym;
   if(g_s[i].hours[0] >= 0)
   {
      MqlDateTime d;
      TimeToStruct(c, d);
      bool ok = false;
      for(int h = 0; h < 2; h++)
         if(g_s[i].hours[h] >= 0 && d.hour == (g_s[i].hours[h] + InpServerMinusNY) % 24) ok = true;
      if(!ok) return;
   }
   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(s, PERIOD_H4, 0, 3, r) != 3 || r[0].time != c) return;
   double hA = r[2].high, lA = r[2].low, hB = r[1].high, lB = r[1].low, cB = r[1].close;
   if(!(lA < cB && cB < hA)) return;
   int dir;
   double sl, tgt;
   if(hB > hA && lB >= lA)      { dir = -1; sl = hB + g_s[i].buf; tgt = lA; }
   else if(lB < lA && hB <= hA) { dir = 1;  sl = lB - g_s[i].buf; tgt = hA; }
   else return;

   int bias = 0;
   if(g_s[i].bias == BIAS_D1)
   {
      int sh = iBarShift(s, PERIOD_D1, DayStart(c) - 1, false);
      if(sh < 0) return;
      double o = iOpen(s, PERIOD_D1, sh), cl = iClose(s, PERIOD_D1, sh);
      bias = cl > o ? 1 : (cl < o ? -1 : 0);
   }
   else if(g_s[i].bias == BIAS_EMA)
   {
      double e[];
      if(CopyBuffer(g_s[i].ema, 0, 1, 1, e) != 1) return;
      bias = cB > e[0] ? 1 : -1;
   }
   else if(g_s[i].bias == BIAS_PREM)
   {
      int sh = iBarShift(s, PERIOD_D1, DayStart(c), true);
      double dayOpen = sh >= 0 ? iOpen(s, PERIOD_D1, sh) : r[0].open;
      bias = r[0].open > dayOpen ? -1 : 1;
   }
   if(g_s[i].bias != BIAS_NONE && bias != dir) return;

   int n = 0;
   for(int k = 0; k < ArraySize(g_set); k++) if(g_set[k].on) g_set[n++] = g_set[k];  // compactar
   ArrayResize(g_set, n + 1);
   g_set[n].on = true; g_set[n].slot = i; g_set[n].dir = dir; g_set[n].sl = sl; g_set[n].tgt = tgt;
   g_set[n].entry = 0; g_set[n].c = c; g_set[n].end = c + 4 * 3600 * g_s[i].window; g_set[n].ticket = 0;
   PrintFormat("%s: setup CRT %s, SL %.5f, objetivo %.5f, ventana hasta %s", g_s[i].name, dir == 1 ? "largo" : "corto",
               sl, tgt, TimeToString(g_set[n].end));
}

//--- abre a mercado (market = true) o pone el límite en g_set[k].entry
bool Enter(int k, bool market)
{
   int i = g_set[k].slot, d = g_set[k].dir;
   string s = g_s[i].sym;
   double sl = g_set[k].sl;
   double price = market ? (d == 1 ? SymbolInfoDouble(s, SYMBOL_ASK) : SymbolInfoDouble(s, SYMBOL_BID)) : g_set[k].entry;
   double risk = MathAbs(price - sl);
   if(risk < 3 * g_s[i].buf || (d == 1 ? price <= sl : price >= sl)) { g_set[k].on = false; return false; }
   double tp = g_s[i].tp == TP_CRT ? g_set[k].tgt : price + d * (g_s[i].tp == TP_1R ? 1 : 2) * risk;
   if(d == 1 ? tp <= price : tp >= price) { g_set[k].on = false; return false; }
   ENUM_ORDER_TYPE t = d == 1 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double money = AllowedRisk();
   double vol = VolumeFor(s, t, price, sl, money);
   if(vol <= 0) { PrintFormat("%s: sin riesgo disponible (%.2f)", g_s[i].name, money); g_set[k].on = false; return false; }
   trade.SetExpertMagicNumber(g_s[i].magic);
   trade.SetTypeFillingBySymbol(s);
   bool ok;
   if(market)
      ok = d == 1 ? trade.Buy(vol, s, 0, NormPrice(s, sl), NormPrice(s, tp), g_s[i].name)
                  : trade.Sell(vol, s, 0, NormPrice(s, sl), NormPrice(s, tp), g_s[i].name);
   else
      ok = d == 1 ? trade.BuyLimit(vol, NormPrice(s, price), s, NormPrice(s, sl), NormPrice(s, tp), ORDER_TIME_GTC, 0, g_s[i].name)
                  : trade.SellLimit(vol, NormPrice(s, price), s, NormPrice(s, sl), NormPrice(s, tp), ORDER_TIME_GTC, 0, g_s[i].name);
   if(!ok)
   {
      PrintFormat("%s: orden rechazada (%d %s)", g_s[i].name, trade.ResultRetcode(), trade.ResultRetcodeDescription());
      g_set[k].on = false;
      return false;
   }
   PrintFormat("%s: %s %s %.2f lotes a %.5f, SL %.5f, TP %.5f, riesgo %.2f", g_s[i].name, market ? "entrada" : "límite",
               d == 1 ? "compra" : "venta", vol, price, sl, tp, money);
   if(market) DropSetups(i);
   else g_set[k].ticket = trade.ResultOrder();
   return true;
}

//--- vela M15 recién cerrada (r[0]) para los setups del símbolo i
void OnM15Close(int i)
{
   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(g_s[i].sym, PERIOD_M15, 1, 6, r) != 6) return;
   for(int k = 0; k < ArraySize(g_set); k++)
   {
      if(!g_set[k].on || g_set[k].slot != i || r[0].time < g_set[k].c) continue;
      if(r[0].time >= g_set[k].end) { DeleteSetup(k); continue; }
      int d = g_set[k].dir;
      double sl = g_set[k].sl, tgt = g_set[k].tgt;
      bool touched = d == -1 ? (r[0].high >= sl || r[0].low <= tgt) : (r[0].low <= sl || r[0].high >= tgt);
      bool touchedTgt = d == -1 ? r[0].low <= tgt : r[0].high >= tgt;
      if(g_s[i].entry == ENTRY_FVG)
      {
         if(g_set[k].entry != 0) { if(touchedTgt) DeleteSetup(k); }
         else if(touched) DeleteSetup(k);
         else if(r[2].time >= g_set[k].c)
         {
            if(d == -1 && r[2].low > r[0].high && tgt < r[0].high && r[0].high < sl) g_set[k].entry = r[0].high;
            if(d == 1 && r[2].high < r[0].low && sl < r[0].low && r[0].low < tgt) g_set[k].entry = r[0].low;
            if(g_set[k].entry != 0) PrintFormat("%s: FVG M15, límite en %.5f", g_s[i].name, g_set[k].entry);
         }
      }
      else
      {
         if(touched) { DeleteSetup(k); continue; }
         double ref = d == -1 ? MathMin(MathMin(r[1].low, r[2].low), MathMin(r[3].low, r[4].low))
                              : MathMax(MathMax(r[1].high, r[2].high), MathMax(r[3].high, r[4].high));
         bool brk = d == -1 ? r[0].close < ref : r[0].close > ref;
         bool inside = d == -1 ? (tgt < r[0].close && r[0].close < sl) : (sl < r[0].close && r[0].close < tgt);
         if(brk && inside)
         {
            if(FridayStop() || NewsBlocked(i)) { PrintFormat("%s: MSS descartado (viernes o noticias)", g_s[i].name); DeleteSetup(k); }
            else Enter(k, true);
            if(FindPosition(i) != 0) return;
            continue;
         }
      }
      // la última M15 de la ventana ya está procesada: fin del setup
      if(g_set[k].on && r[0].time + 15 * 60 >= g_set[k].end) DeleteSetup(k);
   }
}

//--- órdenes límite FVG: cancelar si toca el objetivo, quitar con noticias, reponer después
void ManagePending(int i)
{
   string s = g_s[i].sym;
   double bid = SymbolInfoDouble(s, SYMBOL_BID);
   bool blocked = FridayStop() || NewsBlocked(i);
   for(int k = 0; k < ArraySize(g_set); k++)
   {
      if(!g_set[k].on || g_set[k].slot != i || g_set[k].entry == 0) continue;
      int d = g_set[k].dir;
      if(d == -1 ? bid <= g_set[k].tgt : bid >= g_set[k].tgt) { DeleteSetup(k); continue; }
      if(g_set[k].ticket != 0)
      {
         if(!OrderSelect(g_set[k].ticket)) { g_set[k].ticket = 0; g_set[k].on = false; continue; }  // llenada o borrada
         if(blocked) { trade.OrderDelete(g_set[k].ticket); g_set[k].ticket = 0; }
         continue;
      }
      if(blocked) continue;
      // precio ya más allá del límite (p. ej. tras una noticia): a mercado si sigue antes del SL
      bool crossed = d == -1 ? bid >= g_set[k].entry : SymbolInfoDouble(s, SYMBOL_ASK) <= g_set[k].entry;
      Enter(k, crossed);
      if(FindPosition(i) != 0) return;
   }
}

//+------------------------------------------------------------------+
void Run()
{
   int today = DateKey(TimeTradeServer());
   bool allowed = CheckRules(today);
   for(int i = 0; i < ArraySize(g_s); i++)
   {
      if(!allowed) { DropSetups(i); continue; }
      if(ManagePosition(i)) continue;
      string s = g_s[i].sym;
      datetime h4 = iTime(s, PERIOD_H4, 0), m15 = iTime(s, PERIOD_M15, 0);
      if(h4 == 0 || m15 == 0) continue;
      if(m15 != g_s[i].lastM15)
      {
         if(g_s[i].lastM15 != 0) OnM15Close(i);
         g_s[i].lastM15 = m15;
      }
      if(h4 != g_s[i].lastH4)
      {
         g_s[i].lastH4 = h4;
         // solo al principio de la vela C: si el bot arranca a mitad de vela, se espera a la siguiente
         if(Bars(s, PERIOD_M15, h4, TimeTradeServer()) <= 1 && !FridayStop()) NewSetup(i, h4);
      }
      if(FindPosition(i) == 0) ManagePending(i);
   }
}

//+------------------------------------------------------------------+
//| Registro de operaciones en CSV                                    |
//+------------------------------------------------------------------+
void WriteCsv(string line)
{
   int f = FileOpen(CSV_NAME, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(f == INVALID_HANDLE) return;
   if(FileSize(f) == 0)
      FileWriteString(f, "estrategia;simbolo;lado;entrada;salida;precio_entrada;sl;precio_salida;volumen;beneficio;R\n");
   FileSeek(f, 0, SEEK_END);
   FileWriteString(f, line + "\n");
   FileClose(f);
}

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &req, const MqlTradeResult &res)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || !HistoryDealSelect(trans.deal)) return;
   long magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
   int slot = SlotOfMagic(magic);
   if(slot < 0) return;
   long posId = HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   double price = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
   if(entry == DEAL_ENTRY_IN)
   {
      int k = ArraySize(g_posId);
      ArrayResize(g_posId, k + 1); ArrayResize(g_posEntry, k + 1); ArrayResize(g_posSl, k + 1);
      ArrayResize(g_posTime, k + 1); ArrayResize(g_posDir, k + 1); ArrayResize(g_posMagic, k + 1);
      g_posId[k] = posId;
      g_posEntry[k] = price;
      g_posSl[k] = HistoryDealGetDouble(trans.deal, DEAL_SL);
      g_posTime[k] = (datetime)HistoryDealGetInteger(trans.deal, DEAL_TIME);
      g_posDir[k] = HistoryDealGetInteger(trans.deal, DEAL_TYPE) == DEAL_TYPE_BUY ? 1 : -1;
      g_posMagic[k] = magic;
      DropSetups(slot);  // límite llenado: el resto de setups del símbolo se anulan
      return;
   }
   if(entry != DEAL_ENTRY_OUT) return;
   for(int k = 0; k < ArraySize(g_posId); k++)
   {
      if(g_posId[k] != posId) continue;
      double risk = MathAbs(g_posEntry[k] - g_posSl[k]);
      double r = risk > 0 ? g_posDir[k] * (price - g_posEntry[k]) / risk : 0;
      double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT) + HistoryDealGetDouble(trans.deal, DEAL_COMMISSION)
                      + HistoryDealGetDouble(trans.deal, DEAL_SWAP);
      int dg = (int)SymbolInfoInteger(g_s[slot].sym, SYMBOL_DIGITS);
      WriteCsv(StringFormat("%s;%s;%d;%s;%s;%s;%s;%s;%.2f;%.2f;%.4f", g_s[slot].name, g_s[slot].sym, g_posDir[k],
                            TimeToString(g_posTime[k], TIME_DATE | TIME_SECONDS),
                            TimeToString((datetime)HistoryDealGetInteger(trans.deal, DEAL_TIME), TIME_DATE | TIME_SECONDS),
                            DoubleToString(g_posEntry[k], dg), DoubleToString(g_posSl[k], dg), DoubleToString(price, dg),
                            HistoryDealGetDouble(trans.deal, DEAL_VOLUME), profit, r));
      ArrayRemove(g_posId, k, 1); ArrayRemove(g_posEntry, k, 1); ArrayRemove(g_posSl, k, 1);
      ArrayRemove(g_posTime, k, 1); ArrayRemove(g_posDir, k, 1); ArrayRemove(g_posMagic, k, 1);
      return;
   }
}

//+------------------------------------------------------------------+
int OnInit()
{
   g_tester = (bool)MQLInfoInteger(MQL_TESTER);
   if(!g_tester && AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL && !InpAllowReal)
   {
      Alert("CRTPropBot: la cuenta es REAL y InpAllowReal = false. Actívalo solo en la cuenta de fondeo.");
      return INIT_FAILED;
   }
   ArrayResize(g_s, 0);
   //       nombre      símbolo     colchón     sesión NY  ventana sesgo      entrada    TP     BE
   if(InpUseNAS)  AddSlot("CRT NAS100", InpSymNAS,  InpBufNAS,  -1, -1, 2, BIAS_D1,   ENTRY_FVG, TP_1R,  false, 1);
   if(InpUseSPX)  AddSlot("CRT SPX500", InpSymSPX,  InpBufSPX,   1, -1, 1, BIAS_EMA,  ENTRY_MSS, TP_2R,  true,  2);
   if(InpUseUS30) AddSlot("CRT US30",   InpSymUS30, InpBufUS30,  1, -1, 1, BIAS_D1,   ENTRY_MSS, TP_2R,  false, 3);
   if(InpUseGBP)  AddSlot("CRT GBPUSD", InpSymGBP,  InpBufGBP,   1,  9, 1, BIAS_EMA,  ENTRY_FVG, TP_2R,  true,  4);
   if(InpUseXAU)  AddSlot("CRT XAUUSD", InpSymXAU,  InpBufXAU,   9, -1, 1, BIAS_PREM, ENTRY_MSS, TP_CRT, false, 5);
   if(ArraySize(g_s) == 0) { Alert("CRTPropBot: no hay ningún símbolo activado."); return INIT_FAILED; }
   ArrayResize(g_newsAt, ArraySize(g_s));
   ArrayResize(g_newsBlocked, ArraySize(g_s));
   for(int i = 0; i < ArraySize(g_s); i++)
   {
      g_newsAt[i] = 0; g_newsBlocked[i] = false;
      if(!SymbolSelect(g_s[i].sym, true))
      {
         Alert("CRTPropBot: el símbolo ", g_s[i].sym, " no existe en este bróker. Corrige el nombre en los parámetros.");
         return INIT_FAILED;
      }
      if(g_s[i].bias != BIAS_EMA) continue;
      g_s[i].ema = iMA(g_s[i].sym, PERIOD_H4, 50, 0, MODE_EMA, PRICE_CLOSE);
      if(g_s[i].ema == INVALID_HANDLE) { Alert("CRTPropBot: no se puede crear la EMA50 H4 de ", g_s[i].sym); return INIT_FAILED; }
   }
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
      Print("AVISO: la cuenta no es hedging (no afecta: una posición por símbolo).");
   if(!g_tester)
   {
      int off = (int)MathRound((TimeTradeServer() - TimeGMT()) / 3600.0);
      if(off != 2 && off != 3)
         Alert(StringFormat("CRTPropBot: el servidor va a GMT%+d. El bot asume hora NY + %d (GMT+2 invierno / GMT+3 verano): "
                            "revisa InpServerMinusNY o las sesiones no cuadrarán.", off, InpServerMinusNY));
      // las órdenes pendientes de una sesión anterior se quedan sin setup: se borran
      for(int i = OrdersTotal() - 1; i >= 0; i--)
      {
         ulong tk = OrderGetTicket(i);
         if(tk != 0 && SlotOfMagic(OrderGetInteger(ORDER_MAGIC)) >= 0) trade.OrderDelete(tk);
      }
   }
   ArrayResize(g_set, 0);

   if(InpInitialBalance > 0)
   {
      g_init = InpInitialBalance;
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      if(!InpVerifyMode && (bal > 1.3 * g_init || bal < 0.85 * g_init))
      {
         Alert(StringFormat("CRTPropBot: el saldo (%.2f) no cuadra con InpInitialBalance (%.2f). Los límites diario y de DD "
                            "se calculan sobre el saldo inicial: abre una cuenta de ese tamaño o corrige el parámetro.", bal, g_init));
         return INIT_FAILED;
      }
   }
   else
   {
      if(!GlobalVariableCheck(GV("initial"))) GlobalVariableSet(GV("initial"), AccountInfoDouble(ACCOUNT_BALANCE));
      g_init = GlobalVariableGet(GV("initial"));
   }
   if(g_tester) FileDelete(CSV_NAME, FILE_COMMON);
   trade.SetDeviationInPoints(InpDeviationPts);
   EventSetTimer(1);
   PrintFormat("CRTPropBot: %d símbolos, saldo inicial %.2f, riesgo %.2f (%.2f%%), suelo %.2f, objetivo %s%s",
               ArraySize(g_s), g_init, InpRiskPct / 100 * g_init, InpRiskPct, g_init * (1 - InpMaxDDPct / 100),
               InpTargetPct > 0 ? DoubleToString(g_init * (1 + InpTargetPct / 100), 2) : "ninguno (fondeada)",
               InpVerifyMode ? " (modo verificación)" : "");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   for(int i = 0; i < ArraySize(g_s); i++) if(g_s[i].ema != INVALID_HANDLE) IndicatorRelease(g_s[i].ema);
}

void OnTick()  { Run(); }
void OnTimer() { Run(); }
//+------------------------------------------------------------------+
