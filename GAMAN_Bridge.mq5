//+------------------------------------------------------------------+
//|                                                 GAMAN_Bridge.mq5  |
//|                            Bridge between GAMAN Python and MT5    |
//+------------------------------------------------------------------+
#property copyright "GAMAN Trading"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

// Globale objecten
CTrade trade;

// Settings
input string OrderCommandFile  = "gaman_order.json";
input string OrderResultFile   = "gaman_result.json";
input string HeartbeatFile     = "gaman_heartbeat.json";
input int    MagicNumber       = 20260608;
input int    SlippagePoints    = 10;
input int    PollIntervalMs    = 1000;

// State
datetime last_heartbeat = 0;
string   last_processed_id = "";

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
{
    trade.SetExpertMagicNumber(MagicNumber);
    trade.SetDeviationInPoints(SlippagePoints);
    trade.SetTypeFillingBySymbol(_Symbol);
    
    Print("=================================");
    Print("GAMAN Bridge EA started");
    Print("Magic Number: ", MagicNumber);
    Print("Polling: ", OrderCommandFile);
    Print("=================================");
    
    // Schrijf initial heartbeat
    WriteHeartbeat();
    
    // Set timer voor polling
    EventSetMillisecondTimer(PollIntervalMs);
    
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    Print("GAMAN Bridge EA stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Timer event — main polling loop                                  |
//+------------------------------------------------------------------+
void OnTimer()
{
    // Write heartbeat elke 5 seconden
    if(TimeCurrent() - last_heartbeat > 5) {
        WriteHeartbeat();
        last_heartbeat = TimeCurrent();
    }
    
    // Check voor nieuwe order commando
    CheckForOrderCommand();
}

//+------------------------------------------------------------------+
//| Write heartbeat file zodat GAMAN weet dat EA actief is           |
//+------------------------------------------------------------------+
void WriteHeartbeat()
{
    int handle = FileOpen(HeartbeatFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(handle == INVALID_HANDLE) return;
    
    string json = StringFormat(
        "{\"status\":\"alive\",\"time\":\"%s\",\"account\":%d,\"balance\":%.2f,\"equity\":%.2f,\"open_positions\":%d}",
        TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS),
        AccountInfoInteger(ACCOUNT_LOGIN),
        AccountInfoDouble(ACCOUNT_BALANCE),
        AccountInfoDouble(ACCOUNT_EQUITY),
        PositionsTotal()
    );
    
    FileWriteString(handle, json);
    FileClose(handle);
}

//+------------------------------------------------------------------+
//| Check voor nieuwe order commando vanuit GAMAN                    |
//+------------------------------------------------------------------+
void CheckForOrderCommand()
{
    // Check of commando file bestaat
    if(!FileIsExist(OrderCommandFile)) return;
    
    // Lees file content
    int handle = FileOpen(OrderCommandFile, FILE_READ | FILE_TXT | FILE_ANSI);
    if(handle == INVALID_HANDLE) return;
    
    string content = "";
    while(!FileIsEnding(handle)) {
        content += FileReadString(handle);
    }
    FileClose(handle);
    
    if(StringLen(content) < 10) {
        FileDelete(OrderCommandFile);
        return;
    }
    
    // Parse JSON velden (simple key-value extraction)
    string order_id = ExtractJsonValue(content, "id");
    string action   = ExtractJsonValue(content, "action");
    string symbol   = ExtractJsonValue(content, "symbol");
    string side     = ExtractJsonValue(content, "side");
    double volume   = StringToDouble(ExtractJsonValue(content, "volume"));
    double sl       = StringToDouble(ExtractJsonValue(content, "sl"));
    double tp       = StringToDouble(ExtractJsonValue(content, "tp"));
    string ticket_s = ExtractJsonValue(content, "ticket");
    
    // Check voor duplicate (zelfde ID al verwerkt)
    if(order_id == last_processed_id) {
        FileDelete(OrderCommandFile);
        return;
    }
    
    Print("[GAMAN] Received command: ", action, " ", symbol, " ", side, " volume=", volume);
    
    bool success = false;
    string error_msg = "";
    ulong ticket = 0;
    double actual_entry = 0;
    
    if(action == "OPEN") {
        // Plaats nieuwe order
        ENUM_ORDER_TYPE order_type = (side == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
        
        bool result = trade.PositionOpen(
            symbol,
            order_type,
            volume,
            (order_type == ORDER_TYPE_BUY) ? SymbolInfoDouble(symbol, SYMBOL_ASK) : SymbolInfoDouble(symbol, SYMBOL_BID),
            sl,
            tp,
            "GAMAN_" + order_id
        );
        
        if(result) {
            ticket = trade.ResultOrder();
            actual_entry = trade.ResultPrice();
            success = true;
            Print("[GAMAN] Order opened. Ticket: ", ticket, " Entry: ", actual_entry);
        } else {
            error_msg = "Open failed: " + trade.ResultRetcodeDescription();
            Print("[GAMAN] ", error_msg);
        }
    }
    else if(action == "CLOSE") {
        // Sluit bestaande order
        ulong target_ticket = StringToInteger(ticket_s);
        if(PositionSelectByTicket(target_ticket)) {
            bool result = trade.PositionClose(target_ticket);
            if(result) {
                success = true;
                Print("[GAMAN] Position closed. Ticket: ", target_ticket);
            } else {
                error_msg = "Close failed: " + trade.ResultRetcodeDescription();
            }
        } else {
            error_msg = "Ticket not found: " + ticket_s;
        }
    }
    else if(action == "MODIFY") {
        // Wijzig SL/TP
        ulong target_ticket = StringToInteger(ticket_s);
        if(PositionSelectByTicket(target_ticket)) {
            bool result = trade.PositionModify(target_ticket, sl, tp);
            if(result) {
                success = true;
                Print("[GAMAN] Position modified. Ticket: ", target_ticket);
            } else {
                error_msg = "Modify failed: " + trade.ResultRetcodeDescription();
            }
        }
    }
    
    // Schrijf resultaat terug
    WriteResult(order_id, success, ticket, actual_entry, error_msg);
    
    // Mark als verwerkt
    last_processed_id = order_id;
    
    // Delete commando file (voorkomt re-processing)
    FileDelete(OrderCommandFile);
}

//+------------------------------------------------------------------+
//| Schrijf result terug naar GAMAN                                  |
//+------------------------------------------------------------------+
void WriteResult(string order_id, bool success, ulong ticket, double entry, string error)
{
    int handle = FileOpen(OrderResultFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(handle == INVALID_HANDLE) {
        Print("[GAMAN] Failed to write result file");
        return;
    }
    
    string json = StringFormat(
        "{\"id\":\"%s\",\"success\":%s,\"ticket\":%d,\"entry_price\":%.5f,\"error\":\"%s\",\"time\":\"%s\"}",
        order_id,
        success ? "true" : "false",
        ticket,
        entry,
        error,
        TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS)
    );
    
    FileWriteString(handle, json);
    FileClose(handle);
}

//+------------------------------------------------------------------+
//| Simple JSON value extractor                                      |
//+------------------------------------------------------------------+
string ExtractJsonValue(string json, string key)
{
    string search = "\"" + key + "\":";
    int idx = StringFind(json, search);
    if(idx < 0) return "";
    
    idx += StringLen(search);
    
    // Skip whitespace
    while(idx < StringLen(json) && (StringGetCharacter(json, idx) == ' ' || StringGetCharacter(json, idx) == '\t')) {
        idx++;
    }
    
    // Check if value is string (quoted) or number
    if(idx < StringLen(json) && StringGetCharacter(json, idx) == '"') {
        // String value
        idx++; // skip opening quote
        int end = StringFind(json, "\"", idx);
        if(end < 0) return "";
        return StringSubstr(json, idx, end - idx);
    } else {
        // Number value — read until comma or close brace
        int end = idx;
        while(end < StringLen(json)) {
            ushort c = StringGetCharacter(json, end);
            if(c == ',' || c == '}' || c == ' ' || c == '\n' || c == '\r') break;
            end++;
        }
        return StringSubstr(json, idx, end - idx);
    }
}

//+------------------------------------------------------------------+
//| Tick event — gebruikt voor reconciliation                        |
//+------------------------------------------------------------------+
void OnTick()
{
    // Voor nu: niets. Reconciliation gebeurt via heartbeat polling van GAMAN's kant.
    // Later kunnen we hier price updates loggen naar file als nodig.
}
