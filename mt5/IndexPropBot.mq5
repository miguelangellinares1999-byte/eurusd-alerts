//+------------------------------------------------------------------+
//| IndexPropBot.mq5                                                  |
//| Perfil "equilibrado" de backtest/index_prop_optimize.py:          |
//|  - ORB 30 en NAS100 (1.5% de riesgo)                              |
//|  - RSI(2) diario solo compras en SPX500 y NAS100 (3% de riesgo)   |
//|  - Reglas de prueba de fondeo: +10% objetivo, DD 10%, diario 5%,  |
//|    guarda diaria (80% del límite) y riesgo justo al objetivo.     |
//|                                                                   |
//| ORB: rango 09:30-10:00 NY en M5. Buy stop en el máximo y sell     |
//|  stop en el mínimo (OCO), SL en el otro extremo, TP 2R. Las       |
//|  órdenes se borran a las 12:00 NY y la posición se cierra a las   |
//|  16:00 NY. Se descarta el día si el rango <= 4 spreads.           |
//| RSI(2): a las 16:50 NY con el cierre del día en curso: si cierre  |
//|  > SMA200 y RSI(2) < 10 -> compra, SL 2.5 ATR(14). Salida a la    |
//|  misma hora cuando el cierre supera la SMA5 o tras 10 días.       |
//|                                                                   |
//| El servidor de BlackBull va a hora NY + 7 (también en verano):    |
//| el día de servidor empieza a las 17:00 NY, como el día de trading.|
//| Todas las operaciones cerradas se escriben en                     |
//| Common\Files\IndexPropBot_trades.csv (para backtest/verify_mt5.py)|
//+------------------------------------------------------------------+
#property copyright "eurusd-alerts"
#property version   "1.00"
#property description "ORB 30 NAS100 + RSI(2) diario SPX500/NAS100 con reglas de prueba de fondeo"

#include <Trade/Trade.mqh>

input group "Estrategias"
input string InpOrbSymbol      = "NAS100";        // Símbolo del ORB
input double InpOrbRiskPct     = 1.5;             // Riesgo ORB (% del saldo inicial)
input int    InpOrbMinutes     = 30;              // Minutos del rango de apertura
input double InpOrbTpR         = 2.0;             // TP del ORB en R
input string InpRsiSymbols     = "SPX500,NAS100"; // Símbolos del RSI(2), separados por comas
input double InpRsiRiskPct     = 3.0;             // Riesgo RSI(2) (% del saldo inicial)

input group "Reglas de la prueba de fondeo"
input double InpInitialBalance = 0;      // Saldo inicial de la prueba (0 = saldo al arrancar)
input double InpTargetPct      = 10;     // Objetivo de ganancia %
input double InpMaxDDPct       = 10;     // Drawdown máximo % (estático)
input double InpDailyPct       = 5;      // Pérdida diaria máxima %
input double InpDailyGuard     = 0.8;    // Fracción del límite diario que se puede usar
input bool   InpToTarget       = true;   // Cerca del objetivo, arriesgar solo lo necesario
input double InpMinRiskPct     = 0.25;   // Riesgo mínimo al acercarse al objetivo %

input group "Ejecución"
input int    InpServerMinusNY  = 7;        // Horas que el servidor va por delante de Nueva York
input long   InpMagic          = 20261009; // Magic base (ORB = +1, RSI = +2)
input int    InpDeviationPts   = 50;       // Desviación máxima en órdenes a mercado (puntos)
input bool   InpVerifyMode     = false;    // Verificación: riesgo fijo, sin reglas de fondeo
input bool   InpAllowReal      = false;    // Permitir operar en cuenta REAL

CTrade trade;
string g_rsi[];
double g_init = 0;
double g_dayStart = 0;
int    g_day = -1;
const string CSV_NAME = "IndexPropBot_trades.csv";

// Entradas abiertas, para calcular R al cerrar
long     g_posId[];
double   g_posEntry[], g_posSl[];
datetime g_posTime[];
int      g_posDir[];
long     g_posMagic[];  // magic de la entrada: el cierre puede llevar otro

long MagicOrb() { return InpMagic + 1; }
long MagicRsi() { return InpMagic + 2; }
bool IsOurs(long magic) { return magic == MagicOrb() || magic == MagicRsi(); }
string GV(string key) { return "IPB_" + (string)InpMagic + "_" + key; }

int DateKey(datetime t)
{
   MqlDateTime d;
   TimeToStruct(t, d);
   return d.year * 10000 + d.mon * 100 + d.day;
}

datetime DayStartServer(datetime t)
{
   MqlDateTime d;
   TimeToStruct(t, d);
   d.hour = 0; d.min = 0; d.sec = 0;
   return StructToTime(d);
}

int NYMinutes()
{
   MqlDateTime d;
   TimeToStruct(TimeTradeServer() - InpServerMinusNY * 3600, d);
   return d.hour * 60 + d.min;
}

int NYWeekday()
{
   MqlDateTime d;
   TimeToStruct(TimeTradeServer() - InpServerMinusNY * 3600, d);
   return d.day_of_week;
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

//--- riesgo abierto: posiciones hasta su SL + una pierna de las órdenes ORB pendientes
double OpenRisk()
{
   double risk = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || !IsOurs(PositionGetInteger(POSITION_MAGIC))) continue;
      double sl = PositionGetDouble(POSITION_SL);
      if(sl <= 0) continue;
      ENUM_ORDER_TYPE t = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      risk += MathMax(LossMoney(PositionGetString(POSITION_SYMBOL), t, PositionGetDouble(POSITION_VOLUME),
                                PositionGetDouble(POSITION_PRICE_OPEN), sl), 0);
   }
   double pending = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong tk = OrderGetTicket(i);
      if(tk == 0 || OrderGetInteger(ORDER_MAGIC) != MagicOrb()) continue;
      ENUM_ORDER_TYPE t = OrderGetInteger(ORDER_TYPE) == ORDER_TYPE_BUY_STOP ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      pending = MathMax(pending, LossMoney(OrderGetString(ORDER_SYMBOL), t, OrderGetDouble(ORDER_VOLUME_CURRENT),
                                           OrderGetDouble(ORDER_PRICE_OPEN), OrderGetDouble(ORDER_SL)));
   }
   return risk + pending;
}

//--- dinero a arriesgar en una operación nueva (mismas reglas que backtest/prop_challenge.py)
double AllowedRisk(double basePct, double tpR)
{
   double risk = basePct / 100.0 * g_init;
   if(InpVerifyMode) return risk;
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   if(InpToTarget)
   {
      double needed = (g_init * (1 + InpTargetPct / 100.0) - bal) / (0.8 * tpR);
      risk = MathMin(risk, MathMax(needed, MathMin(InpMinRiskPct / 100.0 * g_init, risk)));
   }
   double room = InpDailyGuard * InpDailyPct / 100.0 * g_init - (g_dayStart - bal) - OpenRisk();
   return MathMin(risk, room);
}

double VolumeFor(string s, ENUM_ORDER_TYPE type, double entry, double sl, double riskMoney)
{
   double perLot = LossMoney(s, type, 1.0, entry, sl);
   if(perLot <= 0 || riskMoney <= 0) return 0;
   double step = SymbolInfoDouble(s, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(s, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(s, SYMBOL_VOLUME_MAX);
   double vol = MathFloor(riskMoney / perLot / step) * step;
   if(vol < vmin)
   {
      PrintFormat("%s: el riesgo %.2f no llega al lote mínimo (%.2f por lote): no se opera", s, riskMoney, perLot);
      return 0;
   }
   return NormalizeDouble(MathMin(vol, vmax), 8);
}

void CloseAll(string why)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk != 0 && IsOurs(PositionGetInteger(POSITION_MAGIC))) trade.PositionClose(tk);
   }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong tk = OrderGetTicket(i);
      if(tk != 0 && IsOurs(OrderGetInteger(ORDER_MAGIC))) trade.OrderDelete(tk);
   }
   Print("Cierre total: ", why);
}

void DeleteOrbOrders()
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong tk = OrderGetTicket(i);
      if(tk != 0 && OrderGetInteger(ORDER_MAGIC) == MagicOrb()) trade.OrderDelete(tk);
   }
}

ulong FindPosition(string s, long magic)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk != 0 && PositionGetInteger(POSITION_MAGIC) == magic && PositionGetString(POSITION_SYMBOL) == s) return tk;
   }
   return 0;
}

//+------------------------------------------------------------------+
//| ORB                                                               |
//+------------------------------------------------------------------+
void ManageOrb(int today)
{
   string s = InpOrbSymbol;
   int m = NYMinutes();
   ulong pos = FindPosition(s, MagicOrb());
   if(m >= 16 * 60 && pos != 0)                             // cierre al final del contado
   {
      trade.SetExpertMagicNumber(MagicOrb());
      trade.PositionClose(pos);
   }
   if(pos != 0 || m >= 12 * 60) DeleteOrbOrders();           // OCO y fin de la ventana de entrada
   int start = 9 * 60 + 30 + InpOrbMinutes;
   if(m < start || m >= 12 * 60 || (int)GlobalVariableGet(GV("orb_day")) == today) return;
   int wd = NYWeekday();
   if(wd == 0 || wd == 6) return;

   datetime t0 = DayStartServer(TimeTradeServer()) + (9 * 60 + 30 + InpServerMinusNY * 60) * 60;
   MqlRates r[];
   int n = CopyRates(s, PERIOD_M5, t0, t0 + InpOrbMinutes * 60 - 1, r);
   if(n != InpOrbMinutes / 5 || r[0].time != t0)
   {
      if(m > start + 15) { GlobalVariableSet(GV("orb_day"), today); Print("ORB: sin velas completas del rango, día descartado"); }
      return;  // las velas pueden tardar unos segundos en llegar
   }
   GlobalVariableSet(GV("orb_day"), today);
   double hi = r[0].high, lo = r[0].low;
   for(int i = 1; i < n; i++) { hi = MathMax(hi, r[i].high); lo = MathMin(lo, r[i].low); }
   double range = hi - lo;
   double spread = SymbolInfoInteger(s, SYMBOL_SPREAD) * SymbolInfoDouble(s, SYMBOL_POINT);
   if(range <= 4 * spread) { Print("ORB: rango demasiado pequeño frente al spread"); return; }

   double risk = AllowedRisk(InpOrbRiskPct, InpOrbTpR);
   double vol = VolumeFor(s, ORDER_TYPE_BUY, hi, lo, risk);
   if(vol <= 0) { PrintFormat("ORB: sin riesgo disponible (%.2f)", risk); return; }
   double ask = SymbolInfoDouble(s, SYMBOL_ASK), bid = SymbolInfoDouble(s, SYMBOL_BID);
   trade.SetExpertMagicNumber(MagicOrb());
   if(ask < hi)
      trade.BuyStop(vol, NormPrice(s, hi), s, NormPrice(s, lo), NormPrice(s, hi + InpOrbTpR * range), ORDER_TIME_GTC, 0, "ORB");
   if(bid > lo)
      trade.SellStop(vol, NormPrice(s, lo), s, NormPrice(s, hi), NormPrice(s, lo - InpOrbTpR * range), ORDER_TIME_GTC, 0, "ORB");
   PrintFormat("ORB %s: rango %.1f-%.1f, %.2f lotes, riesgo %.2f", s, lo, hi, vol, risk);
}

//+------------------------------------------------------------------+
//| RSI(2) diario                                                     |
//+------------------------------------------------------------------+
void ManageRsi(string s, int today)
{
   int m = NYMinutes();
   if(m < 16 * 60 + 50 || m >= 16 * 60 + 59 || (int)GlobalVariableGet(GV("rsi_" + s)) == today) return;
   int wd = NYWeekday();
   if(wd == 0 || wd == 6) return;
   double c[], h[], l[];
   ArraySetAsSeries(c, false); ArraySetAsSeries(h, false); ArraySetAsSeries(l, false);
   int n = CopyClose(s, PERIOD_D1, 0, 300, c);
   if(n < 210 || CopyHigh(s, PERIOD_D1, 0, n, h) != n || CopyLow(s, PERIOD_D1, 0, n, l) != n) return;
   if(DateKey(iTime(s, PERIOD_D1, 0)) != today) return;  // la vela de hoy aún no está
   GlobalVariableSet(GV("rsi_" + s), today);
   int L = n - 1;  // vela de hoy (en curso): su cierre es el precio actual

   double sma200 = 0, sma5 = 0, atr = 0;
   for(int i = L - 199; i <= L; i++) sma200 += c[i];
   sma200 /= 200;
   for(int i = L - 4; i <= L; i++) sma5 += c[i];
   sma5 /= 5;
   for(int i = L - 13; i <= L; i++)
      atr += MathMax(h[i] - l[i], MathMax(MathAbs(h[i] - c[i - 1]), MathAbs(l[i] - c[i - 1])));
   atr /= 14;
   // RSI(2) de Wilder (media exponencial alfa 1/2, como pandas ewm(adjust=False))
   double up = MathMax(c[1] - c[0], 0), dn = MathMax(c[0] - c[1], 0);
   for(int i = 2; i <= L; i++)
   {
      double d = c[i] - c[i - 1];
      up += 0.5 * (MathMax(d, 0) - up);
      dn += 0.5 * (MathMax(-d, 0) - dn);
   }
   double rsi = dn == 0 ? 100 : 100 - 100 / (1 + up / dn);

   ulong pos = FindPosition(s, MagicRsi());
   if(pos != 0)
   {
      PositionSelectByTicket(pos);
      int held = iBarShift(s, PERIOD_D1, (datetime)PositionGetInteger(POSITION_TIME));
      if(c[L] > sma5 || held >= 10)
      {
         trade.SetExpertMagicNumber(MagicRsi());
         trade.PositionClose(pos);
         PrintFormat("RSI2 %s: salida (cierre %.1f, SMA5 %.1f, %d días)", s, c[L], sma5, held);
      }
      return;
   }
   if(!(c[L] > sma200 && rsi < 10)) return;
   double ask = SymbolInfoDouble(s, SYMBOL_ASK);
   double sl = NormPrice(s, c[L] - 2.5 * atr);
   double risk = AllowedRisk(InpRsiRiskPct, 1.0);
   double vol = VolumeFor(s, ORDER_TYPE_BUY, ask, sl, risk);
   if(vol <= 0) { PrintFormat("RSI2 %s: sin riesgo disponible (%.2f)", s, risk); return; }
   trade.SetExpertMagicNumber(MagicRsi());
   if(trade.Buy(vol, s, 0, sl, 0, "RSI2"))
      PrintFormat("RSI2 %s: compra %.2f lotes (RSI %.1f, SL %.1f, riesgo %.2f)", s, vol, rsi, sl, risk);
}

//+------------------------------------------------------------------+
//| Reglas de la prueba                                               |
//+------------------------------------------------------------------+
bool CheckRules(int today)
{
   if(today != g_day)
   {
      g_day = today;
      string k = GV("daystart_" + (string)today);
      if(!GlobalVariableCheck(k)) GlobalVariableSet(k, AccountInfoDouble(ACCOUNT_BALANCE));
      g_dayStart = GlobalVariableGet(k);
   }
   if(InpVerifyMode) return true;
   if(GlobalVariableCheck(GV("finished"))) return false;
   if(GlobalVariableGet(GV("halt_day")) == today) return false;
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   if(eq >= g_init * (1 + InpTargetPct / 100.0))
   {
      CloseAll("objetivo alcanzado");
      GlobalVariableSet(GV("finished"), 1);
      Alert("IndexPropBot: OBJETIVO ALCANZADO. El bot deja de operar.");
      return false;
   }
   if(eq <= g_init * (1 - InpMaxDDPct / 100.0) + 0.002 * g_init)
   {
      CloseAll("drawdown máximo");
      GlobalVariableSet(GV("finished"), -1);
      Alert("IndexPropBot: DRAWDOWN MÁXIMO. El bot deja de operar.");
      return false;
   }
   if(g_dayStart - eq >= 0.95 * InpDailyPct / 100.0 * g_init)
   {
      CloseAll("cerca del límite diario");
      GlobalVariableSet(GV("halt_day"), today);
      return false;
   }
   return true;
}

void Run()
{
   int today = DateKey(TimeTradeServer());
   if(!CheckRules(today)) return;
   ManageOrb(today);
   for(int i = 0; i < ArraySize(g_rsi); i++) ManageRsi(g_rsi[i], today);
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
   if(!IsOurs(magic)) return;
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
      WriteCsv(StringFormat("%s;%s;%d;%s;%s;%.2f;%.2f;%.2f;%.2f;%.2f;%.4f",
                            g_posMagic[k] == MagicOrb() ? "ORB 30" : "RSI2 diario", HistoryDealGetString(trans.deal, DEAL_SYMBOL),
                            g_posDir[k], TimeToString(g_posTime[k], TIME_DATE | TIME_SECONDS),
                            TimeToString((datetime)HistoryDealGetInteger(trans.deal, DEAL_TIME), TIME_DATE | TIME_SECONDS),
                            g_posEntry[k], g_posSl[k], price, HistoryDealGetDouble(trans.deal, DEAL_VOLUME), profit, r));
      ArrayRemove(g_posId, k, 1); ArrayRemove(g_posEntry, k, 1); ArrayRemove(g_posSl, k, 1);
      ArrayRemove(g_posTime, k, 1); ArrayRemove(g_posDir, k, 1); ArrayRemove(g_posMagic, k, 1);
      return;
   }
}

//+------------------------------------------------------------------+
int OnInit()
{
   bool tester = (bool)MQLInfoInteger(MQL_TESTER);
   if(!tester && AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL && !InpAllowReal)
   {
      Alert("IndexPropBot: la cuenta es REAL y InpAllowReal = false. Úsalo en la cuenta de la prueba o en demo.");
      return INIT_FAILED;
   }
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
      Print("AVISO: la cuenta no es hedging; las posiciones ORB y RSI2 de NAS100 se mezclarían.");
   StringSplit(InpRsiSymbols, ',', g_rsi);
   for(int i = 0; i < ArraySize(g_rsi); i++) { StringTrimLeft(g_rsi[i]); StringTrimRight(g_rsi[i]); SymbolSelect(g_rsi[i], true); }
   SymbolSelect(InpOrbSymbol, true);

   if(InpInitialBalance > 0) g_init = InpInitialBalance;
   else
   {
      if(!GlobalVariableCheck(GV("initial"))) GlobalVariableSet(GV("initial"), AccountInfoDouble(ACCOUNT_BALANCE));
      g_init = GlobalVariableGet(GV("initial"));
   }
   if(tester) FileDelete(CSV_NAME, FILE_COMMON);
   trade.SetDeviationInPoints(InpDeviationPts);
   trade.SetTypeFillingBySymbol(InpOrbSymbol);
   EventSetTimer(5);
   PrintFormat("IndexPropBot: saldo inicial %.2f, objetivo %.2f, suelo %.2f%s", g_init, g_init * (1 + InpTargetPct / 100),
               g_init * (1 - InpMaxDDPct / 100), InpVerifyMode ? " (modo verificación)" : "");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTick() { Run(); }
void OnTimer() { Run(); }
//+------------------------------------------------------------------+
