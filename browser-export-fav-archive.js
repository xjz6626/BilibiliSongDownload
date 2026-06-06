(() => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function detectMediaId() {
    const url = new URL(location.href);
    const fromQuery = url.searchParams.get("fid") || url.searchParams.get("media_id") || url.searchParams.get("mlid");
    if (fromQuery) return fromQuery.replace(/^ml/i, "");

    const match = location.href.match(/(?:fid=|media_id=|ml)(\d+)/i);
    return match ? match[1] : "";
  }

  function downloadText(filename, text) {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  async function fetchFavPage(mediaId, page, pageSize) {
    const url = new URL("https://api.bilibili.com/x/v3/fav/resource/list");
    url.search = new URLSearchParams({
      media_id: mediaId,
      pn: String(page),
      ps: String(pageSize),
      order: "mtime",
      type: "0",
      tid: "0",
      platform: "web",
    }).toString();

    const resp = await fetch(url, {
      credentials: "include",
      headers: {
        accept: "application/json, text/plain, */*",
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const data = await resp.json();
    if (data.code !== 0) throw new Error(`${data.code}: ${data.message}`);
    return data.data || {};
  }

  async function main() {
    const detected = detectMediaId();
    const mediaId = prompt("收藏夹 media_id / fid / mlid：", detected);
    if (!mediaId) return;

    const pageSize = 40;
    const firstPage = await fetchFavPage(mediaId, 1, pageSize);
    const total = firstPage.info?.media_count || firstPage.info?.count || firstPage.medias?.length || 0;
    const pageCount = Math.max(1, Math.ceil(total / pageSize));
    const rowsByBvid = new Map();

    function addItems(items) {
      for (const item of items || []) {
        if (!item?.bvid) continue;
        rowsByBvid.set(item.bvid, {
          title: item.title || "",
          bvid: item.bvid,
          aid: item.id || item.aid || "",
          url: `https://www.bilibili.com/video/${item.bvid}`,
        });
      }
    }

    addItems(firstPage.medias);
    console.log(`收藏夹标称 ${total} 个，预计抓取 ${pageCount} 页`);
    console.log(`已抓取第 1/${pageCount} 页，本页 ${(firstPage.medias || []).length} 个，累计 ${rowsByBvid.size} 个`);

    for (let page = 2; page <= pageCount; page += 1) {
      const data = await fetchFavPage(mediaId, page, pageSize);
      addItems(data.medias);
      console.log(`已抓取第 ${page}/${pageCount} 页，本页 ${(data.medias || []).length} 个，累计 ${rowsByBvid.size} 个`);
      await sleep(600);
    }

    const rows = [...rowsByBvid.values()];
    const archive = rows.map((row) => `bilibili ${row.bvid}`).join("\n") + "\n";
    const ids =
      "title\tbvid\taid\turl\n" +
      rows
        .map((row) =>
          [row.title, row.bvid, row.aid, row.url]
            .map((value) => String(value).replace(/\t/g, " ").replace(/\n/g, " "))
            .join("\t"),
        )
        .join("\n") +
      "\n";

    downloadText(`bilibili-fav-${mediaId}-archive.txt`, archive);
    downloadText(`bilibili-fav-${mediaId}-ids.tsv`, ids);

    console.log(`完成：收藏夹标称 ${total} 个，实际导出去重 ${rows.length} 个`);
  }

  main().catch((error) => {
    console.error("抓取失败：", error);
    alert(`抓取失败：${error.message}`);
  });
})();
