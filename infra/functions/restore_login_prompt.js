// CloudFront Function, on every response to the browser.
//
// Lambda function URLs rename a WWW-Authenticate header to
// x-amzn-remapped-www-authenticate, because WWW-Authenticate is reserved for
// AWS's own signing. Without the original header a browser never shows its
// login box for basic auth, so this puts it back.
function handler(event) {
  var headers = event.response.headers;
  var remapped = headers["x-amzn-remapped-www-authenticate"];
  if (remapped) {
    headers["www-authenticate"] = { value: remapped.value };
    delete headers["x-amzn-remapped-www-authenticate"];
  }
  return event.response;
}
