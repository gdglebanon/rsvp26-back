// Install as a bound Google Apps Script on the dedicated Attendees tab.
// Script Properties: RSVP_API_URL, RSVP_WEBHOOK_SECRET, RSVP_SHEET_TAB.
// Add an INSTALLABLE on-edit trigger for curateOnEdit (not a simple trigger).
function curateOnEdit(event) {
  const properties = PropertiesService.getScriptProperties();
  const range = event.range;
  const sheet = range.getSheet();
  if (sheet.getName() !== (properties.getProperty('RSVP_SHEET_TAB') || 'Attendees') ||
      range.getColumn() !== 13 || range.getRow() < 2 ||
      range.getNumRows() !== 1 || range.getNumColumns() !== 1) return;
  const action = String(event.value || '').trim().toLowerCase();
  if (!['invite', 'waitlist', 'reject'].includes(action)) return;
  const values = sheet.getRange(range.getRow(), 1, 1, 12).getValues()[0];
  const body = JSON.stringify({ticketId: String(values[0]), action,
    version: Number(values[11]), requestId: Utilities.getUuid()});
  const timestamp = String(Math.floor(Date.now() / 1000));
  const bytes = Utilities.computeHmacSha256Signature(timestamp + '.' + body,
    properties.getProperty('RSVP_WEBHOOK_SECRET'));
  const signature = bytes.map(byte => ('0' + (byte & 255).toString(16)).slice(-2)).join('');
  const response = UrlFetchApp.fetch(properties.getProperty('RSVP_API_URL').replace(/\/$/, '') + '/api/webhooks/sheets', {
    method: 'post', contentType: 'application/json', payload: body, muteHttpExceptions: true,
    headers: {'X-RSVP-Timestamp': timestamp, 'X-RSVP-Signature': signature}
  });
  const result = response.getResponseCode() < 300 ? 'Accepted; waiting for sync' : 'Failed: ' + response.getContentText();
  sheet.getRange(range.getRow(), 14).setValue(result);
  range.clearContent();
}
