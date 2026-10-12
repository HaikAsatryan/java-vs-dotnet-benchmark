using System.Text.RegularExpressions;
using Ledgerline.Json;
using Xunit;

namespace Ledgerline.Tests;

public class TimestampTests
{
   private static readonly Regex Rfc3339Micros =
      new(@"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", RegexOptions.Compiled);

   [Fact]
   public void Format6_AlwaysExactlySixFractionalDigits_WithZ()
   {
      var samples = new[]
      {
         new DateTime(2026, 6, 6, 0, 0, 0, DateTimeKind.Utc),                              // .000000
         new DateTime(2026, 6, 6, 12, 34, 56, 789, DateTimeKind.Utc),                      // .789000
         new DateTime(2026, 6, 6, 12, 34, 56, DateTimeKind.Utc).AddTicks(1234567 % 10000000), // sub-micro
         new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddTicks(1),                  // 1 tick
      };

      foreach (var s in samples)
      {
         var formatted = Ledgerline.Json.Rfc3339Micros.Format6(s);
         Assert.Matches(Rfc3339Micros, formatted);
      }
   }

   [Fact]
   public void Format6_DefeatsSevenDigitRoundTrip()
   {
      // .NET 'O' produces 7 fractional digits; our format must produce exactly 6.
      var dt = new DateTime(2026, 6, 6, 1, 2, 3, DateTimeKind.Utc).AddTicks(1234567);
      var formatted = Ledgerline.Json.Rfc3339Micros.Format6(dt);

      var fractional = formatted.Split('.')[1].TrimEnd('Z');
      Assert.Equal(6, fractional.Length);
   }

   [Fact]
   public void Format6_ConvertsNonUtcKindToUtc()
   {
      var unspecified = new DateTime(2026, 6, 6, 0, 0, 0, DateTimeKind.Unspecified);
      var formatted = Ledgerline.Json.Rfc3339Micros.Format6(unspecified);
      Assert.EndsWith("Z", formatted);
      Assert.Equal("2026-06-06T00:00:00.000000Z", formatted);
   }
}
