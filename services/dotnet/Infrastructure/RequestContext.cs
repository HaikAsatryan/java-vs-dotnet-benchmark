namespace Ledgerline.Infrastructure;

public static class RequestContext
{
   public const string RequestIdItemKey = "ledger.request_id";
   public const string RequestIdHeader = "X-Request-Id";

   public static string RequestId(this HttpContext ctx) =>
      ctx.Items.TryGetValue(RequestIdItemKey, out var v) && v is string s ? s : string.Empty;
}
