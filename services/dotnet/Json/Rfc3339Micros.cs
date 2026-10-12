using System.Globalization;

namespace Ledgerline.Json;

/// RFC3339 UTC with EXACTLY 6 fractional digits and a 'Z' suffix.
/// .NET 'O' format gives 7 digits; this defeats that. Frozen wire format (spec/openapi.yaml,
/// spec/logging.md): "2026-06-06T00:00:00.000000Z", always padded, never trimmed.
public static class Rfc3339Micros
{
   // Custom format: 6 'f' chars give exactly 6 fractional digits, zero-padded, never trimmed.
   private const string Format = "yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'";

   public static string Format6(DateTime value)
   {
      var utc = value.Kind switch
      {
         DateTimeKind.Utc => value,
         DateTimeKind.Local => value.ToUniversalTime(),
         _ => DateTime.SpecifyKind(value, DateTimeKind.Utc),
      };
      return utc.ToString(Format, CultureInfo.InvariantCulture);
   }

   public static string Format6(DateTimeOffset value) =>
      value.UtcDateTime.ToString(Format, CultureInfo.InvariantCulture);
}
