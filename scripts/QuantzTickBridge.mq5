#property strict
#property version   "1.00"
#property description "Pushes every MT5 OnTick event to the local Quantz web server over a TCP socket."

input string QuantzHost = "127.0.0.1";
input int    QuantzPort = 8787;
input string QuantzPath = "/bridge/tick";
input int    TimeoutMs = 1000;
input int    MinMillisBetweenPosts = 0;

ulong last_post_ms = 0;

int OnInit()
{
   Print("QuantzTickBridge socket posting ticks to ", QuantzHost, ":", QuantzPort, QuantzPath);
   return(INIT_SUCCEEDED);
}

void OnTick()
{
   ulong now_ms = GetTickCount64();
   if(MinMillisBetweenPosts > 0 && now_ms - last_post_ms < (ulong)MinMillisBetweenPosts)
      return;

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double spread_points = point > 0.0 ? (tick.ask - tick.bid) / point : 0.0;

   string path = StringFormat(
      "%s?symbol=%s&bid=%.*f&ask=%.*f&point=%.10f&digits=%d&spread_points=%.2f&tick_time=%I64d",
      QuantzPath,
      _Symbol,
      digits,
      tick.bid,
      digits,
      tick.ask,
      point,
      digits,
      spread_points,
      (long)tick.time
   );

   string request = "GET " + path + " HTTP/1.1\r\n"
                  + "Host: " + QuantzHost + "\r\n"
                  + "Connection: close\r\n"
                  + "User-Agent: QuantzTickBridge\r\n\r\n";

   ResetLastError();
   int socket = SocketCreate();
   if(socket == INVALID_HANDLE)
   {
      Print("QuantzTickBridge SocketCreate failed. error=", GetLastError());
      return;
   }

   if(!SocketConnect(socket, QuantzHost, QuantzPort, TimeoutMs))
   {
      Print("QuantzTickBridge SocketConnect failed. error=", GetLastError());
      SocketClose(socket);
      return;
   }

   uchar req[];
   int len = StringToCharArray(request, req, 0, WHOLE_ARRAY, CP_UTF8) - 1;
   if(len <= 0)
   {
      Print("QuantzTickBridge request encoding failed.");
      SocketClose(socket);
      return;
   }

   ResetLastError();
   int sent = SocketSend(socket, req, len);
   if(sent != len)
   {
      Print("QuantzTickBridge SocketSend failed. sent=", sent, " expected=", len, " error=", GetLastError());
      SocketClose(socket);
      return;
   }

   last_post_ms = now_ms;
   SocketClose(socket);
}
