// Resembl Lambda bootstrap: all queries remain denied until an owner-backed
// authorizer and qualified engine/index custody are supplied.
package main

import (
	"context"
	"github.com/aws/aws-lambda-go/events"
	"github.com/aws/aws-lambda-go/lambda"
	service "github.com/maci0/resembl/service"
	"github.com/maci0/resembl/service/internal/lambdaproxy"
	"log"
	"os"
	"strings"
	"time"
)

func main() {
	endpoint := strings.TrimSpace(os.Getenv("RESEMBL_ENGINE_URL"))
	if endpoint == "" {
		endpoint = "http://127.0.0.1:8081"
	}
	engine, err := service.NewLocalEngine(endpoint)
	if err != nil {
		log.Fatal("invalid engine configuration")
	}
	h, err := service.New(service.DenyAll{}, engine, "unconfigured", 4, 30*time.Second)
	if err != nil {
		log.Fatal("invalid handler configuration")
	}
	lambda.Start(func(ctx context.Context, e events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
		return lambdaproxy.Handle(ctx, h, e)
	})
}
