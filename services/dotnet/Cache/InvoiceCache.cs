using System.Text.Json;
using Ledgerline.Data;
using Ledgerline.Dtos;
using StackExchange.Redis;

namespace Ledgerline.Cache;

/// Cache-aside (spec/cache.md). SERIALIZE-THEN-CACHE: the cached value is the EXACT UTF-8
/// bytes the endpoint returns, so a hit serves byte-equivalent output to a miss.
public sealed class InvoiceCache(IConnectionMultiplexer mux, TimeSpan ttl)
{
   private readonly IDatabase _db = mux.GetDatabase();

   private static string Key(long id) => $"invoice:{id}";

   /// Cache lookup. Hit -> the cached JSON bytes (caller returns them verbatim, no re-serialize).
   /// Miss -> load via repo; absent -> null (NOT cached: no negative caching). Present -> serialize
   /// canonical body, SET with TTL, return the freshly serialized bytes.
   public async Task<byte[]?> GetOrLoadBytesAsync(long id, IInvoiceRepository repo, CancellationToken ct)
   {
      var cached = await _db.StringGetAsync(Key(id));
      if (cached.HasValue)
         return (byte[])cached!;

      var dto = await repo.GetAsync(id, ct);
      if (dto is null)
         return null;

      // Property names come from [JsonPropertyName] on every member, so these bytes are
      // identical to what the HTTP pipeline (Web-default options) writes for the same DTO.
      var bytes = JsonSerializer.SerializeToUtf8Bytes(dto);
      await _db.StringSetAsync(Key(id), bytes, ttl);
      return bytes;
   }

   /// POST /invoices invalidation: DEL invoice:{new_id}. One extra Redis RTT, identical both sides.
   public Task InvalidateAsync(long id, CancellationToken ct) =>
      _db.KeyDeleteAsync(Key(id));

   public async Task<bool> PingAsync()
   {
      try
      {
         await _db.PingAsync();
         return true;
      }
      catch
      {
         return false;
      }
   }
}
