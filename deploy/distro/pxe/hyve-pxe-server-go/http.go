package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"
)

// HTTPServer wraps net/http.Server with logging and health check.
type HTTPServer struct {
	server   *http.Server
	serveDir string
}

// NewHTTPServer creates an HTTP file server with logging middleware and health endpoint.
func NewHTTPServer(serveDir string, port int) *HTTPServer {
	mux := http.NewServeMux()

	// Health endpoint.
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		resp := map[string]string{
			"status":    "ok",
			"service":   "hyve-pxe-server",
			"serve_dir": serveDir,
		}
		json.NewEncoder(w).Encode(resp)
	})

	// File server for everything else.
	fileServer := http.FileServer(http.Dir(serveDir))
	mux.Handle("/", loggingMiddleware(fileServer))

	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", port),
		Handler:      mux,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 300 * time.Second, // Large file transfers (squashfs).
		IdleTimeout:  120 * time.Second,
	}

	return &HTTPServer{
		server:   srv,
		serveDir: serveDir,
	}
}

// ListenAndServe starts the HTTP server.
func (h *HTTPServer) ListenAndServe() error {
	log.Printf("[HTTP] Listening on %s (dir: %s)", h.server.Addr, h.serveDir)
	err := h.server.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

// Shutdown gracefully shuts down the HTTP server using the provided context.
func (h *HTTPServer) Shutdown(ctx context.Context) error {
	return h.server.Shutdown(ctx)
}

// loggingMiddleware wraps an http.Handler to log each request.
func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		lw := &loggingResponseWriter{ResponseWriter: w, statusCode: http.StatusOK}
		next.ServeHTTP(lw, r)
		log.Printf("[HTTP] %s %s %d %s (%s)",
			r.Method, r.URL.Path, lw.statusCode, r.RemoteAddr, time.Since(start))
	})
}

// loggingResponseWriter captures the status code for logging.
type loggingResponseWriter struct {
	http.ResponseWriter
	statusCode int
}

func (lw *loggingResponseWriter) WriteHeader(code int) {
	lw.statusCode = code
	lw.ResponseWriter.WriteHeader(code)
}
