using System.Text.Json;

namespace Ledgerline.Problems;

/// Direct problem+json writer for middleware paths (no Results pipeline available).
/// Emits the same frozen envelope as ProblemDetailsConfig-rewritten responses.
public static class ProblemWriter
{
   public static Task WriteValidation(HttpContext ctx, IReadOnlyDictionary<string, string[]> errors) =>
      Write(ctx, StatusCodes.Status400BadRequest, ProblemStrings.ValidationType,
         ProblemStrings.ValidationTitle, ProblemStrings.ValidationDetail, errors);

   public static Task WriteInternal(HttpContext ctx) =>
      Write(ctx, StatusCodes.Status500InternalServerError, ProblemStrings.InternalType,
         ProblemStrings.InternalTitle, ProblemStrings.InternalDetail, null);

   public static Task WriteUnsupportedMediaType(HttpContext ctx) =>
      Write(ctx, StatusCodes.Status415UnsupportedMediaType, ProblemStrings.UnsupportedType,
         ProblemStrings.UnsupportedTitle, ProblemStrings.UnsupportedDetail, null);

   public static Task WritePayloadTooLarge(HttpContext ctx) =>
      Write(ctx, StatusCodes.Status413PayloadTooLarge, ProblemStrings.TooLargeType,
         ProblemStrings.TooLargeTitle, ProblemStrings.TooLargeDetail, null);

   private static async Task Write(
      HttpContext ctx, int status, string type, string title, string detail,
      IReadOnlyDictionary<string, string[]>? errors)
   {
      ctx.Response.StatusCode = status;
      ctx.Response.ContentType = "application/problem+json";

      using var buffer = new MemoryStream();
      using (var writer = new Utf8JsonWriter(buffer))
      {
         writer.WriteStartObject();
         writer.WriteString("type", type);
         writer.WriteString("title", title);
         writer.WriteNumber("status", status);
         writer.WriteString("detail", detail);
         if (errors is not null)
         {
            writer.WriteStartObject("errors");
            foreach (var (key, messages) in errors)
            {
               writer.WriteStartArray(key);
               foreach (var message in messages)
               {
                  writer.WriteStringValue(message);
               }
               writer.WriteEndArray();
            }
            writer.WriteEndObject();
         }
         writer.WriteEndObject();
      }
      await ctx.Response.Body.WriteAsync(buffer.GetBuffer().AsMemory(0, (int)buffer.Length));
   }
}
