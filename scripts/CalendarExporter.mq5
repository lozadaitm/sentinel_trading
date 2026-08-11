//+------------------------------------------------------------------+
//|                                            CalendarExporter.mq5  |
//|  Exporta el calendario economico de MT5 a JSON para el NewsGuard |
//|  de los bots Python (bot/news.py).                               |
//|                                                                  |
//|  USO:                                                            |
//|   1. Copiar este archivo a MQL5\Experts del terminal (o abrirlo  |
//|      con MetaEditor desde aqui) y compilar (F7).                 |
//|   2. Arrastrarlo a CUALQUIER grafico del terminal conectado.     |
//|      Permitir "Algo Trading" no es necesario: no opera, solo     |
//|      escribe un archivo.                                         |
//|   3. Verifica en la pestaña Expertos el mensaje                  |
//|      "CalendarExporter: N eventos exportados...".                |
//|                                                                  |
//|  El archivo va a la carpeta COMUN de MetaQuotes                  |
//|  (%APPDATA%\MetaQuotes\Terminal\Common\Files\news_calendar.json) |
//|  de modo que UN solo exportador sirve a todas las instancias     |
//|  Python de la maquina. Los timestamps son TimeTradeServer(): la  |
//|  misma base temporal que usa el bot (tick.time), sin zonas       |
//|  horarias de por medio.                                          |
//+------------------------------------------------------------------+
#property copyright "sentinel_trading"
#property version   "1.00"
#property strict

input int    RefreshMinutes = 15;                  // minutos entre refrescos
input int    HorizonHours   = 72;                  // horas de calendario a futuro
input string FileName       = "news_calendar.json";

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(RefreshMinutes * 60);
   Export();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer() { Export(); }

//+------------------------------------------------------------------+
string JsonEscape(const string s)
  {
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\n", " ");
   StringReplace(out, "\r", " ");
   return(out);
  }

string ImportanceTxt(ENUM_CALENDAR_EVENT_IMPORTANCE imp)
  {
   switch(imp)
     {
      case CALENDAR_IMPORTANCE_HIGH:     return("high");
      case CALENDAR_IMPORTANCE_MODERATE: return("moderate");
      case CALENDAR_IMPORTANCE_LOW:      return("low");
     }
   return("none");
  }

//+------------------------------------------------------------------+
void Export()
  {
   datetime from = TimeTradeServer();
   datetime to   = from + HorizonHours * 3600;

   MqlCalendarValue values[];
   if(!CalendarValueHistory(values, from, to))
     {
      PrintFormat("CalendarExporter: CalendarValueHistory fallo (err %d). "
                  "¿Terminal conectado y calendario habilitado?", GetLastError());
      return;
     }

   string body = "";
   int exported = 0;
   for(int i = 0; i < ArraySize(values); i++)
     {
      MqlCalendarEvent ev;
      if(!CalendarEventById(values[i].event_id, ev))
         continue;
      if(ev.importance == CALENDAR_IMPORTANCE_NONE)
         continue;                                  // feriados / notas sin impacto
      MqlCalendarCountry country;
      if(!CalendarCountryById(ev.country_id, country))
         continue;
      if(body != "")
         body += ",";
      body += StringFormat("{\"time\":%I64d,\"currency\":\"%s\",\"importance\":\"%s\",\"name\":\"%s\"}",
                           (long)values[i].time,
                           JsonEscape(country.currency),
                           ImportanceTxt(ev.importance),
                           JsonEscape(ev.name));
      exported++;
     }

   // FILE_COMMON => carpeta comun de MetaQuotes; CP_UTF8 para que Python lo
   // lea con utf-8 sin pelearse con acentos en los nombres de eventos.
   int fh = FileOpen(FileName, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON, ';', CP_UTF8);
   if(fh == INVALID_HANDLE)
     {
      PrintFormat("CalendarExporter: no se pudo abrir %s (err %d)", FileName, GetLastError());
      return;
     }
   FileWriteString(fh, StringFormat("{\"generated_at\":%I64d,\"server_time\":%I64d,\"events\":[%s]}",
                                    (long)TimeTradeServer(), (long)TimeTradeServer(), body));
   FileClose(fh);
   PrintFormat("CalendarExporter: %d eventos exportados a %s (horizonte %dh).",
               exported, FileName, HorizonHours);
  }
//+------------------------------------------------------------------+
