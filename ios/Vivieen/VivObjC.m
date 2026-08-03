#import "VivObjC.h"

@implementation VivObjC

+ (BOOL)catching:(void (^)(void))block {
    @try {
        block();
        return YES;
    } @catch (NSException *problem) {
        NSLog(@"[viv] swallowed: %@", problem.reason);
        return NO;
    }
}

@end
