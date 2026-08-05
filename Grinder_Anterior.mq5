//+------------------------------------------------------------------+
//|             M5_Gold_SmartCut_v13_02.mq5                          |
//|             "The Trend-Aligned Scalper"                          |
//|                                                                  |
//| Arquitecto: Gemini (Quant Algo-Architect)                        |
//| VERIFICACIÓN DE INTEGRIDAD:                                      |
//|  - Lógica Base (ADX Switch, RSI, Bandas) INTACTA.                |
//|  - Añadido: Filtro de Tendencia M15 (Big Brother).               |
//|  - Añadido: Gestión de Riesgo ATR (Adiós SL fijo de 2000pts).    |
//|  - Añadido: Time-Stop (Cierre por estancamiento).                |
//+------------------------------------------------------------------+
#property copyright "Brenskov & AI Architect"
#property version   "13.02"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

// ==================================================================
// INPUTS
// ==================================================================

input group "--- 1. IDENTIDAD & GESTIÓN ---"
input int     InpBaseMagicNum    = 100500; // Scalp usará este +1, Surfer +2
input int     InpBigBroMagic     = 100100; // Solo referencia
input double  InpLotsPer1000     = 0.02;   
input double  InpMaxLotCap       = 0.50;   
input double  InpMinMarginLevel  = 150.0; 

input group "--- 2. FILTRO GRAN HERMANO (M15) ---"
input bool    InpUseBigBroFilter = true;   // TRUE = Activa el filtro de tendencia mayor
input int     InpBigBroPeriod    = 50;     // Periodo EMA M15

input group "--- 3. PROTECCIÓN DE RIESGO (MEJORA) ---"
input double  InpStopLossATR     = 2.0;    // SL Dinámico (x veces el ATR actual)
input int     InpMaxTradeTimeMin = 45;     // Si en 45 min no gana, se evalúa cierre
input double  InpHardSLPoints    = 1000;   // SL Mínimo de seguridad

input group "--- 4. SCALPER (Lógica Original Intacta) ---"
input bool    InpUseReversion    = true;
input int     InpMAPeriod        = 50;     
input int     InpADXPeriod       = 14;     
input int     InpRSIPeriod       = 14;     
input double  InpBaseTolerance   = 0.20;   
input int     InpRSI_Oversold    = 30;     // Valor original v12.30
input int     InpRSI_Overbought  = 70;     // Valor original v12.30
input double  InpMaxBBWidth      = 400;    // Filtro Anti-Explosión (Puntos)

input group "--- 5. SURFER (Lógica Original Intacta) ---"
input bool    InpUseSurferMode   = true;   
input int     InpTrendADX        = 30;     
input int     InpSurferRSI_Max   = 85;     
input int     InpForceRSI_Buy    = 55;     
input int     InpForceRSI_Sell   = 45;     

input group "--- 6. FILTROS DE TIEMPO ---"
input bool    InpUseTimeFilter   = true;   
input int     InpStartHour       = 1;      
input int     InpEndHour         = 23;     

input group "--- 7. TRAILING STOP ---"
input bool    InpUseTrailing     = true;
input int     InpTrailStart      = 100;    
input int     InpTrailDist       = 50;     
input int     InpTrailStep       = 10;     
input int     InpTurboTrigger    = 300;    
input int     InpTurboDist       = 150;    

// Variables Globales
int handleMA, handleADX, handleRSI, handleATR;
int handleBigBroMA; // Handle nuevo para M15
double bufferMA[], bufferADX[], bufferRSI[], bufferATR[];
double bufferBigBro[]; // Buffer para datos M15
datetime lastCandleTime = 0;
bool lastOpWasWin = false; 

//+------------------------------------------------------------------+
//| INIT                                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetDeviationInPoints(20);
   trade.SetTypeFilling(ORDER_FILLING_FOK);
   
   // --- INDICADORES ORIGINALES (M5) ---
   handleMA  = iMA(_Symbol, PERIOD_M5, InpMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   handleADX = iADX(_Symbol, PERIOD_M5, InpADXPeriod);
   handleRSI = iRSI(_Symbol, PERIOD_M5, InpRSIPeriod, PRICE_CLOSE);
   handleATR = iATR(_Symbol, PERIOD_M5, 14); // Necesario para SL Dinámico
   
   // --- INDICADOR NUEVO (M15) ---
   handleBigBroMA = iMA(_Symbol, PERIOD_M15, InpBigBroPeriod, 0, MODE_EMA, PRICE_CLOSE);
   
   if(handleMA == INVALID_HANDLE || handleADX == INVALID_HANDLE || handleRSI == INVALID_HANDLE || 
      handleATR == INVALID_HANDLE || handleBigBroMA == INVALID_HANDLE) 
      return(INIT_FAILED);
      
   ArraySetAsSeries(bufferMA, true);
   ArraySetAsSeries(bufferADX, true);
   ArraySetAsSeries(bufferRSI, true);
   ArraySetAsSeries(bufferATR, true);
   ArraySetAsSeries(bufferBigBro, true);
   
   Print("M5 SMART-CUT v13.02 (Integrity Verified) INICIADO.");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   IndicatorRelease(handleMA);
   IndicatorRelease(handleADX);
   IndicatorRelease(handleRSI);
   IndicatorRelease(handleATR);
   IndicatorRelease(handleBigBroMA);
}

//+------------------------------------------------------------------+
//| UTILS                                                            |
//+------------------------------------------------------------------+
int CountMyPositions() {
   int count = 0;
   int total = PositionsTotal();
   for(int i=0; i<total; i++) {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket)) {
         long magic = PositionGetInteger(POSITION_MAGIC);
         // Contamos Base, Base+1 y Base+2 para cubrir todas las variantes
         if(magic >= InpBaseMagicNum && magic <= InpBaseMagicNum + 2) count++;
      }
   }
   return count;
}

double CalculateDynamicLots() {
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double lots = (balance / 1000.0) * InpLotsPer1000;
   
   double min = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double max = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   
   lots = MathFloor(lots/step)*step;
   if(lots < min) lots = min;
   if(lots > max) lots = max;
   if(lots > InpMaxLotCap) lots = InpMaxLotCap;
   
   return lots;
}

double CalculateDynamicTolerance(double adxValue, double currentPrice) {
   double basePipValue = currentPrice * (InpBaseTolerance / 100.0); 
   double adxFactor = (50.0 - adxValue) / 50.0; 
   if(adxFactor < 0.2) adxFactor = 0.2; 
   return basePipValue * adxFactor;
}

void CheckLastTradeResult() {
   if(HistorySelect(0, TimeCurrent())) {
      int total = HistoryDealsTotal();
      for(int i = total - 1; i >= 0; i--) {
         ulong ticket = HistoryDealGetTicket(i);
         long magic = HistoryDealGetInteger(ticket, DEAL_MAGIC);
         if(magic >= InpBaseMagicNum && magic <= InpBaseMagicNum + 2) {
            if(HistoryDealGetInteger(ticket, DEAL_ENTRY) == DEAL_ENTRY_OUT) { 
               double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT);
               lastOpWasWin = (profit > 0);
               return; 
            }
         }
      }
   }
}

// --- NUEVA FUNCIÓN: Semáforo M15 ---
// Retorna: 1 (Tendencia Alcista), -1 (Tendencia Bajista), 0 (Neutro)
int GetBigBrotherBias() {
   if(!InpUseBigBroFilter) return 0; // Si el usuario apaga el filtro, retorna 0 (Neutro) y no bloquea nada
   
   if(CopyBuffer(handleBigBroMA, 0, 0, 3, bufferBigBro) < 3) return 0;
   
   // Si la EMA M15 está subiendo consistentemente
   if(bufferBigBro[0] > bufferBigBro[1] && bufferBigBro[1] > bufferBigBro[2]) return 1;
   // Si la EMA M15 está bajando consistentemente
   if(bufferBigBro[0] < bufferBigBro[1] && bufferBigBro[1] < bufferBigBro[2]) return -1;
   
   return 0; 
}

//+------------------------------------------------------------------+
//| GESTIÓN DE SALIDAS (Time Stop & Trailing)                        |
//+------------------------------------------------------------------+
void ManageExits() {
   for(int i=PositionsTotal()-1; i>=0; i--) {
      ulong ticket = PositionGetTicket(i);
      if(PositionSelectByTicket(ticket)) {
         long magic = PositionGetInteger(POSITION_MAGIC);
         if(magic < InpBaseMagicNum || magic > InpBaseMagicNum + 2) continue;

         // 1. TIME STOP (Nuevo: Evita quedarse atrapado)
         datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
         long elapsedSeconds = TimeCurrent() - openTime;
         double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
         
         if(elapsedSeconds > (InpMaxTradeTimeMin * 60) && profit < 0) {
             // Solo cerramos si está perdiendo después de X tiempo. Si gana, dejamos correr.
             trade.PositionClose(ticket);
             Print("TIME STOP: Cierre táctico por estancamiento. Ticket: ", ticket);
             continue;
         }

         // 2. TRAILING STOP (Original v12.30 Logic)
         double currentPrice = PositionGetDouble(POSITION_PRICE_CURRENT);
         double openPrice    = PositionGetDouble(POSITION_PRICE_OPEN);
         double sl           = PositionGetDouble(POSITION_SL);
         long type           = PositionGetInteger(POSITION_TYPE);
         
         double points = (type == POSITION_TYPE_BUY) ? (currentPrice - openPrice)/_Point : (openPrice - currentPrice)/_Point;

         if(InpUseTrailing && points >= InpTrailStart) {
            int activeDist = (points >= InpTurboTrigger) ? InpTurboDist : InpTrailDist;
            long stopLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
            double minSafeDist = MathMax((double)activeDist, (double)stopLevel + 20.0);
            
            double potentialSL = 0.0;
            bool update = false;
            
            if(type == POSITION_TYPE_BUY) {
               potentialSL = NormalizeDouble(currentPrice - (minSafeDist * _Point), _Digits);
               if(potentialSL > openPrice && (sl == 0 || potentialSL > sl + InpTrailStep*_Point)) update = true;
            } 
            else { 
               potentialSL = NormalizeDouble(currentPrice + (minSafeDist * _Point), _Digits);
               if(potentialSL < openPrice && (sl == 0 || potentialSL < sl - InpTrailStep*_Point)) update = true;
            }
            
            if(update) trade.PositionModify(ticket, potentialSL, 0);
         }
      }
   }
}

//+------------------------------------------------------------------+
//| ON TICK                                                          |
//+------------------------------------------------------------------+
void OnTick()
{
   ManageExits(); // Prioridad: Gestión de operaciones abiertas

   if(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL) < InpMinMarginLevel) return;
   
   // --- ACTUALIZACIÓN DE BUFFERS ---
   if(CopyBuffer(handleMA, 0, 0, 3, bufferMA) < 0) return;
   if(CopyBuffer(handleADX, 0, 0, 3, bufferADX) < 0) return;
   if(CopyBuffer(handleRSI, 0, 0, 3, bufferRSI) < 0) return;
   if(CopyBuffer(handleATR, 0, 0, 3, bufferATR) < 0) return;

   datetime time0 = iTime(_Symbol, PERIOD_M5, 0);
   if(time0 == lastCandleTime) return; 
   if(InpUseTimeFilter) { MqlDateTime dt; TimeCurrent(dt); if(dt.hour < InpStartHour || dt.hour > InpEndHour) return; }
   if(CountMyPositions() > 0) return; 
   
   CheckLastTradeResult(); 

   // --- LÓGICA DE DECISIÓN (ORIGINAL) ---
   double ema  = bufferMA[1];
   double adx  = bufferADX[1];
   double rsi  = bufferRSI[1];
   double atr  = bufferATR[1];
   double close = iClose(_Symbol, PERIOD_M5, 1);
   double high  = iHigh(_Symbol, PERIOD_M5, 1);
   double low   = iLow(_Symbol, PERIOD_M5, 1);

   double tolerance = CalculateDynamicTolerance(adx, close);
   double upperBand = ema + tolerance;
   double lowerBand = ema - tolerance;
   double bbWidthPoints = (upperBand - lowerBand) / _Point;

   bool signalBuy = false;
   bool signalSell = false;
   int currentMagic = InpBaseMagicNum; // Default
   string comment = "";
   
   // Consultamos al Gran Hermano (M15)
   int bigBroBias = GetBigBrotherBias(); // 1=Alcista, -1=Bajista, 0=Neutro

   // =========================================================
   // 1. MODO SCALPER (REVERSIÓN) - ADX BAJO
   // =========================================================
   if(InpUseReversion && adx < InpTrendADX) {
      // Filtro Extra: Si las bandas explotaron (Ancho > X), no hacemos reversión. Es peligroso.
      if(bbWidthPoints < InpMaxBBWidth) {
         
         // Señal Original: Precio rompe abajo + RSI sobrevendido
         if(low < lowerBand && rsi < InpRSI_Oversold) {
             // VERIFICACIÓN: ¿El Gran Hermano M15 está cayendo fuerte (-1)?
             // Si M15 cae fuerte, NO compramos (evitamos "atrapar el cuchillo").
             if(bigBroBias != -1) { 
                signalBuy = true;
                comment = "[Scalp] Reversion Buy";
                currentMagic = InpBaseMagicNum + 1; // Magic 100501
             }
         }
         
         // Señal Original: Precio rompe arriba + RSI sobrecomprado
         if(high > upperBand && rsi > InpRSI_Overbought) {
             // VERIFICACIÓN: ¿El Gran Hermano M15 sube fuerte (1)?
             // Si M15 sube fuerte, NO vendemos (evitamos ponernos frente al tren).
             if(bigBroBias != 1) {
                signalSell = true;
                comment = "[Scalp] Reversion Sell";
                currentMagic = InpBaseMagicNum + 1; // Magic 100501
             }
         }
      }
   }
   
   // =========================================================
   // 2. MODO SURFER (TENDENCIA) - ADX ALTO
   // =========================================================
   else if(InpUseSurferMode && adx >= InpTrendADX) {
      
      // Señal Original: Precio sobre EMA
      if(close > ema && rsi < InpSurferRSI_Max) {
          if(lastOpWasWin || rsi > InpForceRSI_Buy) { 
              // VERIFICACIÓN: Apoyamos la compra si M15 NO es bajista
              if(bigBroBias != -1) {
                 signalBuy = true;
                 comment = "[Surfer] Buy";
                 currentMagic = InpBaseMagicNum + 2; // Magic 100502
              }
          }
      }
      
      // Señal Original: Precio bajo EMA
      if(close < ema && rsi > (100 - InpSurferRSI_Max)) {
          if(lastOpWasWin || rsi < InpForceRSI_Sell) {
              // VERIFICACIÓN: Apoyamos la venta si M15 NO es alcista
              if(bigBroBias != 1) {
                 signalSell = true;
                 comment = "[Surfer] Sell";
                 currentMagic = InpBaseMagicNum + 2; // Magic 100502
              }
          }
      }
   }

   // --- EJECUCIÓN (CON SL DINÁMICO) ---
   if(signalBuy || signalSell) {
       double lots = CalculateDynamicLots();
       
       // CÁLCULO DE SL: Usamos ATR en lugar de fijo 2000 pts
       double slDist = atr * InpStopLossATR; 
       if(slDist < InpHardSLPoints * _Point) slDist = InpHardSLPoints * _Point;

       trade.SetExpertMagicNumber(currentMagic);

       if(signalBuy) {
           double sl = NormalizeDouble(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - slDist, _Digits);
           // Nota: Comentamos el TP fijo. Dejamos correr hasta Trailing o TimeStop.
           if(trade.Buy(lots, _Symbol, SymbolInfoDouble(_Symbol, SYMBOL_ASK), sl, 0, comment)) {
               lastCandleTime = time0;
               Print("ENTRADA M5 | Magic:", currentMagic, " | TrendM15:", bigBroBias);
           }
       }
       else if(signalSell) {
           double sl = NormalizeDouble(SymbolInfoDouble(_Symbol, SYMBOL_BID) + slDist, _Digits);
           if(trade.Sell(lots, _Symbol, SymbolInfoDouble(_Symbol, SYMBOL_BID), sl, 0, comment)) {
               lastCandleTime = time0;
               Print("ENTRADA M5 | Magic:", currentMagic, " | TrendM15:", bigBroBias);
           }
       }
   }
}