chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "downloadExport" && message.url) {
      chrome.downloads.download({
        url: message.url,
          filename: "audit-catalogue-industria/" + message.filename,
        conflictAction: "overwrite",
        saveAs: false
      });

    sendResponse({ success: true });
  }
});
