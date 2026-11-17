//
//  KTVHCHTTPHLSResponse.m
//  KTVHTTPCache
//
//  Created by Gary Zhao on 2024/1/7.
//  Copyright © 2024 Single. All rights reserved.
//

#import "KTVHCHTTPHLSResponse.h"
#import "KTVHCHTTPConnection.h"
#import "KTVHCDataRangePlan.h"
#import "KTVHCDataUnitPool.h"
#import "KTVHCDownload.h"
#import "KTVHCHLSTool.h"
#import "KTVHCLog.h"

@interface KTVHCHTTPHLSResponse ()

@property (nonatomic, weak) KTVHCHTTPConnection *connection;

@property (nonatomic) UInt64 readedLength;
@property (nonatomic, strong) NSData *data;
@property (nonatomic, strong) KTVHCDataUnit *unit;
@property (nonatomic, strong) KTVHCDataRangePlan *rangePlan;
@property (nonatomic, strong) NSURLSessionDataTask *task;

@end

@implementation KTVHCHTTPHLSResponse

- (instancetype)initWithConnection:(KTVHCHTTPConnection *)connection dataRequest:(KTVHCDataRequest *)dataRequest
{
    if (self = [super init]) {
        KTVHCLogAlloc(self);
        KTVHCLogHTTPHLSResponse(@"%p, Create response\nrequest : %@", self, dataRequest);
        self.connection = connection;
        self.unit = [[KTVHCDataUnitPool pool] unitWithURL:dataRequest.URL];
        // The manifest shares the same range mapping as file resources.
        self.rangePlan = [[KTVHCDataRangePlanner planner] planWithRequest:dataRequest unit:self.unit];
        NSURL *completeURL = self.unit.completeURL;
        if (completeURL) {
            self.data = [NSData dataWithContentsOfURL:completeURL];
        } else {
            __weak typeof(self) weakSelf = self;
            self.task = [[KTVHCHLSTool tool] taskWithURL:dataRequest.URL completionHandler:^(NSData *data, NSError *error) {
                __strong typeof(weakSelf) strongSelf = weakSelf;
                [strongSelf handleResponeWithData:data error:error];
            }];
            [self.task resume];
        }
    }
    return self;
}

- (void)dealloc
{
    [self.unit workingRelease];
    [self.task cancel];
    KTVHCLogDealloc(self);
}

#pragma mark - HTTPResponse

- (void)handleResponeWithData:(NSData *)data error:(NSError *)error
{
    if (error || data.length == 0) {
        [self.connection responseDidAbort:self];
        KTVHCLogHTTPHLSResponse(@"%p, Handle response error: %@", self, error);
    } else {
        self.data = data;
        [self.connection responseHasAvailableData:self];
    }
}

- (long long)ensureLength
{
    if (self.unit.totalLength > 0) {
        return self.unit.totalLength;
    }
    return (long long)self.data.length;
}

- (UInt64)contentOffset
{
    KTVHCRange range = [self.rangePlan responseRangeWithTotalLength:[self ensureLength]];
    if (range.start == KTVHCNotFound || range.start <= 0) {
        return 0;
    }
    return (UInt64)range.start;
}

- (NSData *)readDataOfLength:(NSUInteger)length
{
    UInt64 contentLength = [self contentLength];
    UInt64 location = [self contentOffset] + self.readedLength;
    UInt64 remaining = contentLength > self.readedLength ? contentLength - self.readedLength : 0;
    NSUInteger readLength = (NSUInteger)MIN((UInt64)length, remaining);
    if (location >= self.data.length) {
        KTVHCLogHTTPHLSResponse(@"%p, Read data did finished", self);
        return nil;
    }
    readLength = (NSUInteger)MIN((UInt64)readLength, self.data.length - location);
    NSData *data = [self.data subdataWithRange:NSMakeRange((NSUInteger)location, readLength)];
    self.readedLength += data.length;
    KTVHCLogHTTPHLSResponse(@"%p, Read data : %lld", self, (long long)data.length);
    return data;
}

- (BOOL)delayResponseHeaders
{
    KTVHCLogHTTPHLSResponse(@"%p, Delay response : %d", self, self.self.data.length == 0);
    return self.data.length == 0;
}

- (UInt64)contentLength
{
    KTVHCRange range = [self.rangePlan responseRangeWithTotalLength:[self ensureLength]];
    long long length = KTVHCRangeGetLength(range);
    if (length == KTVHCNotFound || length < 0) {
        length = (long long)self.data.length;
    }
    KTVHCLogHTTPHLSResponse(@"%p, Conetnt length : %lld", self, length);
    return (UInt64)length;
}

- (NSDictionary *)httpHeaders
{
    NSDictionary *headers = [self.rangePlan HTTPHeadersWithBaseHeaders:self.unit.responseHeaders];
    KTVHCLogHTTPHLSResponse(@"%p, Header\n%@", self, headers);
    return headers;
}

- (UInt64)offset
{
    UInt64 offset = [self contentOffset] + self.readedLength;
    KTVHCLogHTTPHLSResponse(@"%p, Offset : %lld", self, (long long)offset);
    return offset;
}

- (void)setOffset:(UInt64)offset
{
    KTVHCLogHTTPHLSResponse(@"%p, Set offset : %lld, %ld", self, offset, self.data.length);
}

- (BOOL)isDone
{
    BOOL done = self.data.length > 0 && self.readedLength >= [self contentLength];
    KTVHCLogHTTPHLSResponse(@"%p, Check done : %d", self, done);
    return done;
}

- (void)connectionDidClose
{
    KTVHCLogHTTPHLSResponse(@"%p, Connection did closed : %lld, %lld", self, self.unit.totalLength, self.unit.cacheLength);
    [self.task cancel];
}

@end
