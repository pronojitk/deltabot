package com.example.deltabot.ui.main

import android.annotation.SuppressLint
import android.content.Context
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.navigation3.runtime.NavKey

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun MainScreen(
  onItemClick: (NavKey) -> Unit,
  modifier: Modifier = Modifier,
) {
  val context = LocalContext.current
  val sharedPref = remember { context.getSharedPreferences("deltabot_prefs", Context.MODE_PRIVATE) }
  
  var savedUrl by remember { mutableStateOf(sharedPref.getString("server_url", "") ?: "") }
  var inputUrl by remember { mutableStateOf(if (savedUrl.isEmpty()) "http://10.0.2.2:5000" else savedUrl) }
  var showInput by remember { mutableStateOf(savedUrl.isEmpty()) }

  if (showInput) {
    Box(
      modifier = Modifier
        .fillMaxSize()
        .background(Color(0xFF02040A))
        .padding(24.dp),
      contentAlignment = Alignment.Center
    ) {
      Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp),
        modifier = Modifier.fillMaxWidth()
      ) {
        Text(
          text = "DeltaBot Mobile Client",
          style = MaterialTheme.typography.headlineMedium,
          color = Color(0xFFF3F4F6)
        )
        Text(
          text = "Enter your DeltaBot server address below.",
          style = MaterialTheme.typography.bodyMedium,
          color = Color(0xFF9CA3AF)
        )
        OutlinedTextField(
          value = inputUrl,
          onValueChange = { inputUrl = it },
          label = { Text("Server URL") },
          modifier = Modifier.fillMaxWidth(),
          colors = OutlinedTextFieldDefaults.colors(
            focusedBorderColor = Color(0xFF6366F1),
            unfocusedBorderColor = Color(0xFF4B5563),
            focusedLabelColor = Color(0xFF6366F1),
            unfocusedLabelColor = Color(0xFF9CA3AF),
            focusedTextColor = Color(0xFFF3F4F6),
            unfocusedTextColor = Color(0xFFF3F4F6)
          )
        )
        Button(
          onClick = {
            if (inputUrl.isNotBlank()) {
              sharedPref.edit().putString("server_url", inputUrl).apply()
              savedUrl = inputUrl
              showInput = false
            }
          },
          modifier = Modifier.fillMaxWidth(),
          colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF6366F1))
        ) {
          Text("Connect", color = Color.White)
        }
        Text(
          text = "Tip: Use http://10.0.2.2:5000 inside Android emulator to connect to localhost.",
          style = MaterialTheme.typography.bodySmall,
          color = Color(0xFF4B5563)
        )
      }
    }
  } else {
    Box(modifier = Modifier.fillMaxSize().background(Color(0xFF02040A))) {
      AndroidView(
        factory = { ctx ->
          WebView(ctx).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            settings.builtInZoomControls = true
            settings.displayZoomControls = false
            webViewClient = WebViewClient()
            loadUrl(savedUrl)
          }
        },
        update = { webView ->
          if (webView.url != savedUrl) {
            webView.loadUrl(savedUrl)
          }
        },
        modifier = Modifier.fillMaxSize()
      )
      
      // Floating button to change settings
      Button(
        onClick = { showInput = true },
        modifier = Modifier
          .align(Alignment.BottomEnd)
          .padding(16.dp),
        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF1E1B4B).copy(alpha = 0.8f))
      ) {
        Text("Change URL", color = Color(0xFF6366F1))
      }
    }
  }
}
