package com.cloudradio.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.net.URL;

public class MainActivity extends android.app.Activity {

    private static final String DEFAULT_URL = "http://43.142.107.253:8088/";
    private static final String PREFS = "cloudradio";
    private static final String KEY_URL = "server_url";

    private WebView web;
    private FrameLayout root;
    private String serverHost = "";
    private long lastBackPress = 0L;
    private boolean pageFailed = false;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Window window = getWindow();
        window.setStatusBarColor(Color.parseColor("#f4efe6"));
        window.setNavigationBarColor(Color.parseColor("#201b17"));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            window.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        }

        root = new FrameLayout(this);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#f4efe6"));

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setUserAgentString(s.getUserAgentString() + " CloudRadioApp/1.0");

        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new WebChromeClient());

        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        addSettingsButton();

        String url = prefs().getString(KEY_URL, DEFAULT_URL);
        load(url);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7);
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private void load(String url) {
        pageFailed = false;
        try {
            URL u = new URL(url);
            serverHost = u.getHost() == null ? "" : u.getHost();
        } catch (Exception e) {
            serverHost = "";
        }
        web.loadUrl(url);
    }

    /* ---------------- 右上角设置入口 ---------------- */
    private void addSettingsButton() {
        TextView gear = new TextView(this);
        gear.setText("⚙");
        gear.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        gear.setTextColor(Color.parseColor("#f4efe6"));
        gear.setGravity(Gravity.CENTER);
        GradientDrawable bg = new GradientDrawable();
        bg.setShape(GradientDrawable.OVAL);
        bg.setColor(Color.parseColor("#B3201B17"));
        gear.setBackground(bg);
        gear.setAlpha(0.62f);

        int size = dp(38);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(size, size);
        lp.gravity = Gravity.TOP | Gravity.END;
        lp.topMargin = dp(10);
        lp.rightMargin = dp(10);
        root.addView(gear, lp);

        gear.setOnClickListener(v -> showSettings());
    }

    private void showSettings() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        box.setPadding(pad, dp(8), pad, 0);

        TextView hint = new TextView(this);
        hint.setText("填写后端地址，例如 http://43.142.107.253:8088/");
        hint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        hint.setTextColor(Color.parseColor("#8b8177"));
        box.addView(hint);

        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(prefs().getString(KEY_URL, DEFAULT_URL));
        input.setSelection(input.getText().length());
        box.addView(input);

        new AlertDialog.Builder(this)
                .setTitle("服务器设置")
                .setView(box)
                .setPositiveButton("保存并重载", (d, w) -> {
                    String url = input.getText().toString().trim();
                    if (url.isEmpty()) {
                        url = DEFAULT_URL;
                    }
                    if (!url.startsWith("http://") && !url.startsWith("https://")) {
                        url = "http://" + url;
                    }
                    prefs().edit().putString(KEY_URL, url).apply();
                    load(url);
                })
                .setNeutralButton("刷新页面", (d, w) -> web.reload())
                .setNegativeButton("取消", null)
                .show();
    }

    /* ---------------- 页面访问控制 ---------------- */
    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            String host = u.getHost();
            String scheme = u.getScheme() == null ? "" : u.getScheme();
            if (!scheme.startsWith("http")) {
                return true;
            }
            if (host != null && host.equals(serverHost)) {
                return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            } catch (Exception ignored) {
            }
            return true;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            injectBridge();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                pageFailed = true;
                view.loadDataWithBaseURL(null, errorPage(), "text/html", "utf-8", null);
            }
        }
    }

    /** 让网页把播放状态同步给原生的前台服务，实现后台/锁屏持续播放 */
    private static final String BRIDGE_JS =
            "(function(){if(window.__cnrBridge)return;window.__cnrBridge=1;" +
            "var P=window.HTMLMediaElement&&HTMLMediaElement.prototype;if(!P)return;" +
            "var send=function(v){try{AndroidBridge.setPlaying(!!v);}catch(e){}};" +
            "var _play=P.play,_pause=P.pause;" +
            "P.play=function(){send(true);return _play.apply(this,arguments);};" +
            "P.pause=function(){send(false);return _pause.apply(this,arguments);};" +
            "document.addEventListener('visibilitychange',function(){if(!document.hidden)send(!document.querySelector('audio')||!document.querySelector('audio').paused);});" +
            "})();";

    private void injectBridge() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT) {
            web.evaluateJavascript(BRIDGE_JS, null);
        }
    }

    public class Bridge {
        @JavascriptInterface
        public void setPlaying(final boolean playing) {
            runOnUiThread(() -> {
                Intent i = new Intent(MainActivity.this, PlaybackService.class);
                if (playing) {
                    try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(i);
                        } else {
                            startService(i);
                        }
                    } catch (Exception ignored) {
                    }
                } else {
                    try {
                        stopService(i);
                    } catch (Exception ignored) {
                    }
                }
            });
        }

        @JavascriptInterface
        public void reload() {
            runOnUiThread(() -> web.reload());
        }
    }

    private String errorPage() {
        String url = prefs().getString(KEY_URL, DEFAULT_URL);
        return "<!doctype html><html><head><meta charset='utf-8'>"
                + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{background:#f4efe6;color:#1f1b18;font-family:system-ui,-apple-system,'PingFang SC',sans-serif;"
                + "padding:56px 26px;text-align:center} h1{font-size:18px;margin:0 0 10px}"
                + "p{color:#8b8177;font-size:13.5px;line-height:1.7} code{background:#fffcf7;border:1px solid #e6dccc;"
                + "border-radius:6px;padding:2px 6px;font-size:12px}"
                + "button{margin-top:22px;background:#201b17;color:#fff6ea;border:0;border-radius:999px;"
                + "padding:12px 26px;font-size:14px}</style></head><body>"
                + "<h1>连不上收音机服务</h1>"
                + "<p>当前地址：<code>" + url + "</code></p>"
                + "<p>请确认后端已启动、端口已放行，手机与服务器网络互通。<br>"
                + "地址填错可点右上角 ⚙ 修改。</p>"
                + "<button onclick=\"AndroidBridge.reload()\">重新连接</button>"
                + "</body></html>";
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (!pageFailed && web.canGoBack()) {
                web.goBack();
                return true;
            }
            long now = System.currentTimeMillis();
            if (now - lastBackPress < 2000) {
                finish();
            } else {
                lastBackPress = now;
                Toast.makeText(this, "再按一次退出", Toast.LENGTH_SHORT).show();
            }
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.removeJavascriptInterface("AndroidBridge");
            try {
                stopService(new Intent(this, PlaybackService.class));
            } catch (Exception ignored) {
            }
            web.destroy();
        }
        super.onDestroy();
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
