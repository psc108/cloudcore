package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"time"
)

// NotFoundError is returned when the API responds with 404.
type NotFoundError struct {
	Path string
}

func (e *NotFoundError) Error() string {
	return fmt.Sprintf("resource not found: %s", e.Path)
}

type Client struct {
	apiURL     string
	apiToken   string
	httpClient *http.Client
	maxRetries int
}

type Option func(*Client)

func WithTimeout(d time.Duration) Option {
	return func(c *Client) { c.httpClient.Timeout = d }
}

func WithRetries(n int) Option {
	return func(c *Client) { c.maxRetries = n }
}

func New(apiURL, apiToken string, opts ...Option) *Client {
	c := &Client{
		apiURL:     apiURL,
		apiToken:   apiToken,
		httpClient: &http.Client{Timeout: 30 * time.Second},
		maxRetries: 3,
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

func (c *Client) do(ctx context.Context, method, path string, body, out any) error {
	var rawBody []byte
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("marshal request: %w", err)
		}
		rawBody = b
	}

	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		if attempt > 0 {
			// Exponential backoff with jitter: 1s, 2s, 4s + up to 500ms jitter.
			backoff := time.Duration(1<<uint(attempt-1))*time.Second + time.Duration(rand.Intn(500))*time.Millisecond
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(backoff):
			}
		}

		var bodyReader io.Reader
		if rawBody != nil {
			bodyReader = bytes.NewReader(rawBody)
		}

		req, err := http.NewRequestWithContext(ctx, method, c.apiURL+path, bodyReader)
		if err != nil {
			return fmt.Errorf("build request: %w", err)
		}
		req.Header.Set("Authorization", "Bearer "+c.apiToken)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/json")

		resp, err := c.httpClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("execute request: %w", err)
			continue
		}

		respBody, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			lastErr = fmt.Errorf("read response: %w", err)
			continue
		}

		if resp.StatusCode == 404 {
			return &NotFoundError{Path: path}
		}
		// Retry on 429 and 5xx.
		if resp.StatusCode == 429 || resp.StatusCode >= 500 {
			lastErr = fmt.Errorf("API error %d: %s", resp.StatusCode, string(respBody))
			continue
		}
		if resp.StatusCode >= 400 {
			return fmt.Errorf("API error %d: %s", resp.StatusCode, string(respBody))
		}

		if out != nil && len(respBody) > 0 {
			if err := json.Unmarshal(respBody, out); err != nil {
				return fmt.Errorf("unmarshal response: %w", err)
			}
		}
		return nil
	}
	return fmt.Errorf("request failed after %d attempts: %w", c.maxRetries+1, lastErr)
}

func (c *Client) Get(ctx context.Context, path string, out any) error {
	return c.do(ctx, http.MethodGet, path, nil, out)
}

func (c *Client) Post(ctx context.Context, path string, body, out any) error {
	return c.do(ctx, http.MethodPost, path, body, out)
}

func (c *Client) Put(ctx context.Context, path string, body, out any) error {
	return c.do(ctx, http.MethodPut, path, body, out)
}

func (c *Client) Delete(ctx context.Context, path string) error {
	return c.do(ctx, http.MethodDelete, path, nil, nil)
}
