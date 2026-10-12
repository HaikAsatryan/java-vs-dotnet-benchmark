using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Mvc.Testing;
using Xunit;
using Xunit.Abstractions;

namespace Ledgerline.Tests;

/// Boots the real app via WebApplicationFactory and captures ACTUAL 400 validation bodies, so the
/// emitted errors-map KEY format is documented from ground truth (the Java side aligns to it).
/// Validation short-circuits before any DB/Redis touch, so no live infra is required.
public class ValidationBodyCaptureTests(ITestOutputHelper output) : IClassFixture<WebApplicationFactory<Program>>
{
   private static WebApplicationFactory<Program> Factory() =>
      new WebApplicationFactory<Program>()
         .WithWebHostBuilder(b => b.UseSetting("REDIS_ADDR", "localhost:6379"));

   [Fact]
   public async Task PricingQuote_InvalidItems_400_ErrorsMapKeyFormat()
   {
      using var factory = Factory();
      using var client = factory.CreateClient();

      // One bad sku in items[2], one out-of-range qty in items[0]; rest valid (need 50 items).
      var items = new List<object>();
      for (var i = 0; i < 50; i++)
         items.Add(new { sku = "EL-0001", qty = 1, unit_price_minor = 100 });
      items[0] = new { sku = "EL-0001", qty = 0, unit_price_minor = 100 };       // qty range
      items[2] = new { sku = "bad-sku", qty = 1, unit_price_minor = 100 };       // sku pattern

      var payload = new { currency = "usd", items };   // currency also invalid (lowercase)
      var resp = await client.PostAsJsonAsync("/pricing/quote", payload);
      var body = await resp.Content.ReadAsStringAsync();

      output.WriteLine($"STATUS: {(int)resp.StatusCode}");
      output.WriteLine($"CONTENT-TYPE: {resp.Content.Headers.ContentType}");
      output.WriteLine($"BODY: {body}");

      Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
      using var doc = JsonDocument.Parse(body);
      Assert.True(doc.RootElement.TryGetProperty("errors", out var errors), "expected errors map");
      foreach (var p in errors.EnumerateObject())
         output.WriteLine($"ERROR KEY: '{p.Name}' VALUES: {p.Value.GetRawText()}");

      // FROZEN emitted key format: PascalCase CLR names; indexed nested path Items[i].Prop.
      Assert.Equal("must be between 1 and 1000", errors.GetProperty("Items[0].Qty")[0].GetString());
      Assert.Equal("must match ^[A-Z]{2}-[0-9]{4}$", errors.GetProperty("Items[2].Sku")[0].GetString());
      Assert.Equal("must match ^[A-Z]{3}$", errors.GetProperty("Currency")[0].GetString());
   }

   [Fact]
   public async Task CreateInvoice_InvalidItems_400_ErrorsMapKeyFormat()
   {
      using var factory = Factory();
      using var client = factory.CreateClient();

      var payload = new
      {
         customer_id = 0,                      // id_positive
         currency = "us",                      // currency pattern
         status = "closed",                    // status enum
         items = new object[]
         {
            new { sku = "bad", description = "", qty = 0, unit_price_minor = -1 },
            new { sku = "EL-0001", description = "ok", qty = 1, unit_price_minor = 100 },
         },
      };

      var resp = await client.PostAsJsonAsync("/invoices", payload);
      var body = await resp.Content.ReadAsStringAsync();

      output.WriteLine($"STATUS: {(int)resp.StatusCode}");
      output.WriteLine($"CONTENT-TYPE: {resp.Content.Headers.ContentType}");
      output.WriteLine($"BODY: {body}");

      Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
      using var doc = JsonDocument.Parse(body);

      // Frozen envelope strings.
      Assert.Equal("https://ledgerline.test/problems/validation", doc.RootElement.GetProperty("type").GetString());
      Assert.Equal("Validation failed", doc.RootElement.GetProperty("title").GetString());
      Assert.Equal(400, doc.RootElement.GetProperty("status").GetInt32());
      Assert.Equal("The request payload failed validation.", doc.RootElement.GetProperty("detail").GetString());

      Assert.True(doc.RootElement.TryGetProperty("errors", out var errors), "expected errors map");
      foreach (var p in errors.EnumerateObject())
         output.WriteLine($"ERROR KEY: '{p.Name}' VALUES: {p.Value.GetRawText()}");

      // Top-level scalar keys are bare PascalCase; collection-element keys are Items[i].Prop.
      Assert.Equal("must be a positive integer", errors.GetProperty("CustomerId")[0].GetString());
      Assert.Equal("must match ^[A-Z]{3}$", errors.GetProperty("Currency")[0].GetString());
      Assert.Equal("must be one of: draft, open", errors.GetProperty("Status")[0].GetString());
      Assert.Equal("must match ^[A-Z]{2}-[0-9]{4}$", errors.GetProperty("Items[0].Sku")[0].GetString());
      Assert.Equal("is required", errors.GetProperty("Items[0].Description")[0].GetString());
      Assert.Equal("must be between 0 and 100000000", errors.GetProperty("Items[0].UnitPriceMinor")[0].GetString());
   }

   [Fact]
   public async Task CreateInvoice_MalformedJsonBody_Frozen400BodyRequired()
   {
      using var factory = Factory();
      using var client = factory.CreateClient();

      // Undeserializable body -> IExceptionHandler path -> frozen 400 {"errors":{"body":["is required"]}}.
      using var content = new StringContent("{\"customer_id\": 1,", Encoding.UTF8, "application/json");
      var resp = await client.PostAsync("/invoices", content);
      var body = await resp.Content.ReadAsStringAsync();

      output.WriteLine($"STATUS: {(int)resp.StatusCode}");
      output.WriteLine($"BODY: {body}");

      Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
      Assert.Equal("application/problem+json", resp.Content.Headers.ContentType?.MediaType);
      using var doc = JsonDocument.Parse(body);
      Assert.Equal("https://ledgerline.test/problems/validation", doc.RootElement.GetProperty("type").GetString());
      Assert.Equal(400, doc.RootElement.GetProperty("status").GetInt32());
      Assert.Equal("is required", doc.RootElement.GetProperty("errors").GetProperty("body")[0].GetString());
   }

   [Fact]
   public async Task GetInvoice_NonPositiveId_400()
   {
      using var factory = Factory();
      using var client = factory.CreateClient();

      var resp = await client.GetAsync("/invoices/0");
      var body = await resp.Content.ReadAsStringAsync();
      output.WriteLine($"STATUS: {(int)resp.StatusCode}");
      output.WriteLine($"BODY: {body}");

      Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
   }

   [Fact]
   public async Task MalformedBody_ProductionEnvironment_FrozenBodyRequired400()
   {
      // The container runs with the default (Production) environment, where
      // RouteHandlerOptions.ThrowOnBadRequest is false unless Program.cs pins it true.
      // This asserts the frozen malformed-body 400 contract OUTSIDE Development,
      // the environment the measured run actually uses.
      using var factory = new WebApplicationFactory<Program>()
         .WithWebHostBuilder(b =>
         {
            b.UseSetting("REDIS_ADDR", "localhost:6379");
            b.UseSetting("environment", "Production");
         });
      using var client = factory.CreateClient();

      using var content = new StringContent("{not valid json", Encoding.UTF8, "application/json");
      var resp = await client.PostAsync("/invoices", content);
      var body = await resp.Content.ReadAsStringAsync();

      output.WriteLine($"STATUS: {(int)resp.StatusCode}");
      output.WriteLine($"BODY: {body}");

      Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
      Assert.Equal("application/problem+json", resp.Content.Headers.ContentType?.MediaType);
      using var doc = JsonDocument.Parse(body);
      Assert.Equal("https://ledgerline.test/problems/validation", doc.RootElement.GetProperty("type").GetString());
      Assert.Equal("is required", doc.RootElement.GetProperty("errors").GetProperty("body")[0].GetString());
   }

   [Fact]
   public async Task NonJsonContentType_Frozen415()
   {
      // MediaTypeGateMiddleware: same frozen grammar as the Java side's JsonMediaTypeGate.
      using var factory = Factory();
      using var client = factory.CreateClient();

      using var content = new StringContent("{}", Encoding.UTF8, "text/plain");
      var resp = await client.PostAsync("/invoices", content);
      var body = await resp.Content.ReadAsStringAsync();
      output.WriteLine($"STATUS: {(int)resp.StatusCode} BODY: {body}");

      Assert.Equal(HttpStatusCode.UnsupportedMediaType, resp.StatusCode);
      Assert.Equal("application/problem+json", resp.Content.Headers.ContentType?.MediaType);
      using var doc = JsonDocument.Parse(body);
      Assert.Equal("https://ledgerline.test/problems/unsupported-media-type",
         doc.RootElement.GetProperty("type").GetString());
      Assert.Equal("The request content type must be application/json.",
         doc.RootElement.GetProperty("detail").GetString());
   }

   [Fact]
   public async Task BodylessPost_NoContentType_FrozenBodyRequired400()
   {
      // Content-Length 0 skips the gate; binding then emits the frozen 400; the
      // Java gate skips body-less POSTs the same way and binding 400s there too.
      using var factory = Factory();
      using var client = factory.CreateClient();

      using var req = new HttpRequestMessage(HttpMethod.Post, "/invoices") { Content = null };
      var resp = await client.SendAsync(req);
      var body = await resp.Content.ReadAsStringAsync();
      output.WriteLine($"STATUS: {(int)resp.StatusCode} BODY: {body}");

      Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
      using var doc = JsonDocument.Parse(body);
      Assert.Equal("is required", doc.RootElement.GetProperty("errors").GetProperty("body")[0].GetString());
   }

   [Fact]
   public void GateGrammar_MatchesFrozenVectors()
   {
      // The frozen gate-grammar vector table (spec/openapi.yaml media-type gate);
      // the Java side pins the same vectors against JsonMediaTypeGate.
      var vectors = new (string? ct, bool ok)[]
      {
         ("application/json", true),
         ("application/json; charset=utf-8", true),
         ("application/problem+json", true),
         ("APPLICATION/JSON", true),
         (" application/json ", true),
         ("application/json; charset", true),   // params never validated
         ("+json", false),
         ("text/json", false),
         ("text/plain", false),
         ("", false),
         (null, false),
      };
      foreach (var (ct, ok) in vectors)
         Assert.Equal(ok, Infrastructure.MediaTypeGateMiddleware.IsJsonMediaType(ct));
   }

   [Fact]
   public async Task Healthz_StaticOk()
   {
      using var factory = Factory();
      using var client = factory.CreateClient();

      var resp = await client.GetAsync("/healthz");
      var body = await resp.Content.ReadAsStringAsync();
      output.WriteLine($"BODY: {body}");
      Assert.Equal(HttpStatusCode.OK, resp.StatusCode);
      Assert.Equal("{\"status\":\"ok\"}", body);
   }

   [Fact]
   public async Task PricingQuote_Valid_50Items_200_ByteShape()
   {
      using var factory = Factory();
      using var client = factory.CreateClient();

      var items = new List<object>();
      for (var i = 0; i < 50; i++)
         items.Add(new { sku = "EL-0042", qty = 10, unit_price_minor = 10000 });

      var resp = await client.PostAsJsonAsync("/pricing/quote", new { currency = "USD", items });
      var body = await resp.Content.ReadAsStringAsync();
      output.WriteLine($"STATUS: {(int)resp.StatusCode}");
      output.WriteLine($"BODY (first 400): {body[..Math.Min(400, body.Length)]}");

      Assert.Equal(HttpStatusCode.OK, resp.StatusCode);
      using var doc = JsonDocument.Parse(body);
      // snake_case wire keys present.
      Assert.True(doc.RootElement.TryGetProperty("subtotal_minor", out _));
      Assert.True(doc.RootElement.TryGetProperty("order_discount_minor", out _));
      Assert.True(doc.RootElement.GetProperty("lines")[0].TryGetProperty("category_surcharge_minor", out _));
   }
}
